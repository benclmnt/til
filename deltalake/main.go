package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"slices"
	"strings"
)

type objectStorage interface {
	// must be atomic.
	putIfAbsent(name string, bytes []byte) error
	listPrefix(prefix string) ([]string, error)
	read(name string) ([]byte, error)
}

type fileObjectStorage struct {
	basedir string
}

func newFileObjectStorage(basedir string) *fileObjectStorage {
	return &fileObjectStorage{basedir}
}

func (s *fileObjectStorage) putIfAbsent(name string, bytes []byte) error {
	tmpfilename := path.Join(s.basedir, uuidv4())
	f, err := os.OpenFile(tmpfilename, os.O_WRONLY|os.O_CREATE, 0644)
	if err != nil {
		return err
	}
	written := 0
	bufSize := 1024 * 16 // 16MB
	for written < len(bytes) {
		n, err := f.Write(bytes[written : written+bufSize])
		if err != nil {
			removeErr := os.Remove(tmpfilename)
			assert(removeErr == nil, fmt.Sprintf("could not remove %s: %s", tmpfilename, removeErr))
			return err
		}
		written += n
	}

	err = f.Sync()
	if err != nil {
		removeErr := os.Remove(tmpfilename)
		assert(removeErr == nil, fmt.Sprintf("could not remove %s: %s", tmpfilename, removeErr))
		return err
	}

	err = f.Close()
	if err != nil {
		removeErr := os.Remove(tmpfilename)
		assert(removeErr == nil, fmt.Sprintf("could not remove %s: %s", tmpfilename, removeErr))
		return err
	}

	filename := path.Join(s.basedir, name)
	err = os.Link(tmpfilename, filename)
	if err != nil {
		removeErr := os.Remove(tmpfilename)
		assert(removeErr == nil, fmt.Sprintf("could not remove %s: %s", tmpfilename, removeErr))
		return err
	}

	return nil
}

func (s *fileObjectStorage) listPrefix(prefix string) ([]string, error) {
	dir, err := os.Open(s.basedir)
	if err != nil {
		return nil, err
	}

	var files []string
	names, err := dir.Readdirnames(-1)
	if err != nil && err != io.EOF {
		return nil, err
	}
	for _, name := range names {
		if strings.HasPrefix(name, prefix) {
			files = append(files, name)
		}
	}

	err = dir.Close()
	return files, err
}

func (s *fileObjectStorage) read(name string) ([]byte, error) {
	filename := path.Join(s.basedir, name)
	return os.ReadFile(filename)
}

type DataobjectAction struct {
	Name  string
	Table string
}

type ChangeMetadataAction struct {
	Table   string
	Columns []string
}

type Action struct {
	AddDataobject  *DataobjectAction
	ChangeMetadata *ChangeMetadataAction
}

const DATAOBJECT_SIZE = 1024

type transaction struct {
	Id int

	// mapping table name to a list of actions on the table.
	previousActions map[string][]Action
	Actions         map[string][]Action

	// Mapping tables to column names
	tables map[string][]string

	// Mapping table name to unflushed/in-memory rows. When rows
	// are flushed, the dataobject that contains them is added to
	// `tx.actions` above and `tx.unflushedDataPointer[table]` is
	// reset to `0`.
	unflushedData        map[string]*[DATAOBJECT_SIZE][]any
	unflushedDataPointer map[string]int
}

type client struct {
	os objectStorage
	// Current transaction, if any. Only one transaction per
	// client at a time. All reads and writes must be within a
	// transaction.
	tx *transaction
}

func newClient(os objectStorage) *client {
	return &client{os, nil}
}

func (c *client) newTx() error {
	if c.tx != nil {
		return errExistingTx
	}

	logPrefix := "_log_"
	txLogFilenames, err := c.os.listPrefix(logPrefix)
	if err != nil {
		return err
	}

	tx := &transaction{
		previousActions:      make(map[string][]Action),
		Actions:              make(map[string][]Action),
		tables:               make(map[string][]string),
		unflushedData:        make(map[string]*[DATAOBJECT_SIZE][]any),
		unflushedDataPointer: make(map[string]int),
	}

	for _, txLogFilename := range txLogFilenames {
		bytes, err := c.os.read(txLogFilename)
		if err != nil {
			return err
		}

		var oldTx transaction
		err = json.Unmarshal(bytes, &oldTx)
		if err != nil {
			return err
		}

		// Transaction metadata files are sorted
		// lexicographically so that the most recent
		// transaction (i.e. the one with the largest
		// transaction id) will be last and tx.Id will end up
		// 1 greater than the most recent transaction ID we
		// see on disk.
		tx.Id = oldTx.Id + 1

		for table, actions := range oldTx.Actions {
			for _, action := range actions {
				if action.AddDataobject != nil {
					tx.previousActions[table] = append(tx.previousActions[table], action)
				} else if action.ChangeMetadata != nil {
					// Store the latest version of each table in memory for easy lookup.
					mtd := action.ChangeMetadata
					tx.tables[table] = mtd.Columns
				} else {
					panic(fmt.Sprintf("unsupported action: %v", action))
				}
			}
		}
	}

	c.tx = tx

	return nil
}

func (c *client) createTable(table string, columns []string) error {
	if c.tx == nil {
		return errNoTx
	}

	if _, exists := c.tx.tables[table]; exists {
		return errTableExists
	}

	// Store it in memory
	c.tx.tables[table] = columns

	// also add it to the aciton history for future transactions
	c.tx.Actions[table] = append(c.tx.Actions[table], Action{
		ChangeMetadata: &ChangeMetadataAction{table, columns},
	})

	return nil
}

func (c *client) writeRow(table string, row []any) error {
	if c.tx == nil {
		return errNoTx
	}

	if _, exists := c.tx.tables[table]; !exists {
		return errNoTable
	}

	// Try to find an unflushed/in-memory data object for this table
	pointer, ok := c.tx.unflushedDataPointer[table]
	if !ok {
		c.tx.unflushedDataPointer[table] = 0
		c.tx.unflushedData[table] = &[DATAOBJECT_SIZE][]any{}
	}

	if pointer == DATAOBJECT_SIZE {
		c.flushRows(table)
		pointer = 0
	}

	c.tx.unflushedData[table][pointer] = row
	c.tx.unflushedDataPointer[table]++
	return nil
}

type dataobject struct {
	Table string
	Name  string
	Data  [DATAOBJECT_SIZE][]any
	Len   int
}

func (c *client) flushRows(table string) error {
	if c.tx == nil {
		return errNoTx
	}

	pointer, exists := c.tx.unflushedDataPointer[table]
	if !exists || pointer == 0 {
		return nil
	}

	df := dataobject{
		Table: table,
		Name:  uuidv4(),
		Data:  *c.tx.unflushedData[table],
		Len:   pointer,
	}
	bytes, err := json.Marshal(df)
	if err != nil {
		return err
	}

	err = c.os.putIfAbsent(fmt.Sprintf("_table_%s_%s", table, df.Name), bytes)
	if err != nil {
		return err
	}

	c.tx.Actions[table] = append(c.tx.Actions[table], Action{
		AddDataobject: &DataobjectAction{
			Name:  df.Name,
			Table: table,
		},
	})

	c.tx.unflushedDataPointer[table] = 0
	return nil
}

func (c *client) commit() error {
	if c.tx == nil {
		return errNoTx
	}

	// flush any outstanding data
	for table := range c.tx.tables {
		err := c.flushRows(table)
		if err != nil {
			c.tx = nil
			return err
		}
	}

	wrote := false
	for _, actions := range c.tx.Actions {
		if len(actions) > 0 {
			wrote = true
			break
		}
	}

	// Read only transaction no concurrency check.
	if !wrote {
		c.tx = nil
		return nil
	}

	filename := fmt.Sprintf("_log_%020d", c.tx.Id)
	// We won't store previous actions, they will be recovered on
	// new transactions. So unset them. Honestly not totally
	// clear why.
	c.tx.previousActions = nil
	bytes, err := json.Marshal(c.tx)
	if err != nil {
		c.tx = nil
		return err
	}

	err = c.os.putIfAbsent(filename, bytes)
	c.tx = nil
	return err
}

var (
	errExistingTx  = fmt.Errorf("Existing Transaction")
	errNoTx        = fmt.Errorf("No Transaction")
	errTableExists = fmt.Errorf("Table Exists")
	errNoTable     = fmt.Errorf("No Such Table")
)

func assert(b bool, msg string) {
	if !b {
		panic(msg)
	}
}

func assertEq[C comparable](a C, b C, prefix string) {
	if a != b {
		panic(fmt.Sprintf("%s '%v' != '%v'", prefix, a, b))
	}
}

var DEBUG = slices.Contains(os.Args, "--debug")

func debug(a ...any) {
	if !DEBUG {
		return
	}

	args := append([]any{"[DEBUG]"}, a...)
	fmt.Println(args...)
}

// https://datatracker.ietf.org/doc/html/rfc4122#section-4.4
func uuidv4() string {
	f, err := os.Open("/dev/random")
	assert(err == nil, fmt.Sprintf("could not open /dev/random: %s", err))
	defer f.Close()

	buf := make([]byte, 16)
	n, err := f.Read(buf)
	assert(err == nil, fmt.Sprintf("could not read 16 bytes from /dev/random: %s", err))
	assert(n == len(buf), "expected 16 bytes from /dev/random")

	// Set bit 6 to 0
	buf[8] &= ^(byte(1) << 6)
	// Set bit 7 to 1
	buf[8] |= 1 << 7

	// Set version
	buf[6] &= ^(byte(1) << 4)
	buf[6] &= ^(byte(1) << 5)
	buf[6] |= 1 << 6
	buf[6] &= ^(byte(1) << 7)

	return fmt.Sprintf("%x-%x-%x-%x-%x",
		buf[:4],
		buf[4:6],
		buf[6:8],
		buf[8:10],
		buf[10:16])
}
