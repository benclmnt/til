package main

import (
	"fmt"
	"os"
	"slices"

	"github.com/tidwall/btree"
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

// a snapshot of value at a given time
type Value struct {
	txStartId uint64
	txEndId   uint64
	value     string
}

type TransactionState uint8

const (
	InProgressTransaction TransactionState = iota
	AbortedTransaction
	CommittedTransaction
)

// Loosest isolation at the top, strictest isolation at the bottom.
type IsolationLevel uint8

const (
	ReadUncommittedIsolation IsolationLevel = iota
	ReadCommittedIsolation
	RepeatableReadIsolation
	SnapshotIsolation
	SerializableIsolation
)

type Transaction struct {
	isolation IsolationLevel
	id        uint64
	state     TransactionState

	// Used only by Repeatable Read and stricter.
	inprogress btree.Set[uint64]

	// Used only by Snapshot Isolation and stricter.
	writeset btree.Set[string]
	readset  btree.Set[string]
}

type Database struct {
	defaultIsolation  IsolationLevel
	store             map[string][]Value
	transactions      btree.Map[uint64, Transaction]
	nextTransactionId uint64
}

func newDatabase() Database {
	return Database{
		defaultIsolation: ReadCommittedIsolation,
		store:            map[string][]Value{},
		// The `0` transaction id will be used to mean that
		// the id was not set. So all valid transaction ids
		// must start at 1.
		nextTransactionId: 1,
	}
}

func (d *Database) inprogress() btree.Set[uint64] {
	var ids btree.Set[uint64]
	iter := d.transactions.Iter()
	for ok := iter.First(); ok; ok = iter.Next() {
		if iter.Value().state == InProgressTransaction {
			ids.Insert(iter.Key())
		}
	}
	return ids
}

func (d *Database) newTransaction() *Transaction {
	t := Transaction{}
	t.isolation = d.defaultIsolation
	t.state = InProgressTransaction

	// Assign and increment transaction id.
	t.id = d.nextTransactionId
	d.nextTransactionId++

	// Store all inprogress transaction ids.
	t.inprogress = d.inprogress()

	// Add this transaction to history.
	d.transactions.Set(t.id, t)

	debug("starting transaction", t.id)

	return &t
}

func (d *Database) completeTransaction(t *Transaction, state TransactionState) error {
	debug("completing transaction ", t.id)

	if state == CommittedTransaction {
		// Check for conflicts.

		// Snapshot Isolation imposes the additional constraint that
		// no transaction A may commit after writing any of the same
		// keys as transaction B has written and committed during
		// transaction A's life.
		if t.isolation == SnapshotIsolation && d.hasConflict(t, func(t1, t2 *Transaction) bool {
			// Check if the transaction has written to any key that
			// another transaction has read or written.
			return haveSharedItem(t1.writeset, t2.writeset)
		}) {
			d.completeTransaction(t, AbortedTransaction)
			return fmt.Errorf("write-write conflict")
		}

		// Serializable Isolation imposes the additional constraint that
		// no transaction A may commit after reading any of the same
		// keys as transaction B has written and committed during
		// transaction A's life, or vice-versa.
		if t.isolation == SerializableIsolation && d.hasConflict(t, func(t1, t2 *Transaction) bool {
			// Check if the transaction has written to any key that
			// another transaction has read or written.
			return haveSharedItem(t1.readset, t2.writeset) || haveSharedItem(t1.writeset, t2.readset)
		}) {
			d.completeTransaction(t, AbortedTransaction)
			return fmt.Errorf("read-write conflict")
		}
	}

	// Update transactions.
	t.state = state
	d.transactions.Set(t.id, *t)

	return nil
}

func (d *Database) transactionState(txId uint64) Transaction {
	t, ok := d.transactions.Get(txId)
	assert(ok, "valid transaction")
	return t
}

func (d *Database) assertValidTransaction(t *Transaction) {
	assert(t.id > 0, "valid id")
	assert(d.transactionState(t.id).state == InProgressTransaction, "in progress")
}

func (d *Database) isvisible(t *Transaction, v Value) bool {
	if t.isolation == ReadUncommittedIsolation {
		return v.txEndId == 0
	}
	if t.isolation == ReadCommittedIsolation {
		// should not see values not created by self that is not committed
		if v.txStartId != t.id && d.transactionState(v.txStartId).state != CommittedTransaction {
			return false
		}

		// should not see values that have been deleted by self or other committed transactions
		if v.txEndId > 0 && (v.txEndId == t.id || d.transactionState(v.txEndId).state == CommittedTransaction) {
			return false
		}

		return true
	}
	// Repeatable Read, Snapshot Isolation, and Serializable
	// further restricts Read Committed so only versions from
	// transactions that completed before this one started are
	// visible.

	// Snapshot Isolation and Serializable will do additional
	// checks at commit time.
	assert(t.isolation == RepeatableReadIsolation ||
		t.isolation == SnapshotIsolation ||
		t.isolation == SerializableIsolation, "invalid isolation level")

	// ignore values created from transactions started after this one.
	if v.txStartId > t.id {
		return false
	}

	// ignore values created from transactions in progress when this one started
	if t.inprogress.Contains(v.txStartId) {
		return false
	}

	// values created by aborted transactions should not be visible.
	if d.transactionState(v.txStartId).state == AbortedTransaction {
		return false
	}

	// values created by our own transaction is visible.

	if v.txEndId > 0 && // deleted / deleting state
		v.txEndId <= t.id && // only consider result from transactions that started before this one or this one
		!t.inprogress.Contains(v.txEndId) && // only consider result from transactions not in progress when this one started
		d.transactionState(v.txEndId).state == CommittedTransaction { // those transactions must be committed
		return false
	}

	if t.isolation == RepeatableReadIsolation {
		return true
	}
	// if t.isolation == RepeatableReadIsolation {
	// 	return v.txEndId <= t.id || !t.inprogress.Contains(v.txStartId)
	// }
	// if t.isolation == SnapshotIsolation {
	// 	return v.txEndId <= t.id || !t.writeset.Contains(v.value)
	// }
	// if t.isolation == SerializableIsolation {
	// 	return v.txEndId <= t.id
	// }
	panic("unreachable")
}

func (d *Database) hasConflict(t1 *Transaction, conflictFn func(*Transaction, *Transaction) bool) bool {
	iter := d.transactions.Iter()

	// iterate over inprogress transactions
	inprogressIter := t1.inprogress.Iter()
	for ok := inprogressIter.First(); ok; ok = inprogressIter.Next() {
		id := inprogressIter.Key()
		found := iter.Seek(id)
		assert(found, "found")
		t2 := iter.Value()
		if t2.state == CommittedTransaction {
			if conflictFn(t1, &t2) {
				return true
			}
		}
	}

	// iterate over all transactions that after before this one
	for id := t1.id; id < d.nextTransactionId; id++ {
		found := iter.Seek(id)
		assert(found, "found")
		t2 := iter.Value()
		if t2.state == CommittedTransaction {
			if conflictFn(t1, &t2) {
				return true
			}
		}
	}

	return false
}

type Connection struct {
	tx *Transaction
	db *Database
}

func (c *Connection) execCommand(command string, args []string) (string, error) {
	debug(command, args)

	if command == "begin" {
		assertEq(c.tx, nil, "no transaction")
		c.tx = c.db.newTransaction()
		c.db.assertValidTransaction(c.tx)
		return fmt.Sprintf("%d", c.tx.id), nil
	}
	if command == "abort" {
		c.db.assertValidTransaction(c.tx)
		err := c.db.completeTransaction(c.tx, AbortedTransaction)
		c.tx = nil
		return "", err
	}
	if command == "commit" {
		c.db.assertValidTransaction(c.tx)
		err := c.db.completeTransaction(c.tx, CommittedTransaction)
		c.tx = nil
		return "", err
	}
	if command == "get" {
		c.db.assertValidTransaction(c.tx)
		key := args[0]
		c.tx.readset.Insert(key)
		for i := len(c.db.store[key]) - 1; i >= 0; i-- {
			v := c.db.store[key][i]
			debug(v, c.tx, c.db.isvisible(c.tx, v))
			if c.db.isvisible(c.tx, v) {
				return v.value, nil
			}
		}
		return "", fmt.Errorf("key not found")
	}
	if command == "delete" || command == "set" {
		c.db.assertValidTransaction(c.tx)
		key := args[0]

		// mark all visible versions as now invalid (why?)
		found := false
		for i := len(c.db.store[key]) - 1; i >= 0; i-- {
			v := &c.db.store[key][i]
			if c.db.isvisible(c.tx, *v) {
				// assertEq(v.txEndId, 0, "end id") set the txEndId to all value if it is visible?
				v.txEndId = c.tx.id
				found = true
			}
		}
		if command == "delete" && !found {
			return "", fmt.Errorf("key not found")
		}
		c.tx.writeset.Insert(key)
		// add a new version if it's a set command
		if command == "set" {
			value := args[1]
			c.db.store[key] = append(c.db.store[key], Value{
				txStartId: c.tx.id,
				txEndId:   0,
				value:     value,
			})

			return value, nil
		}
		// delete ok
		return "", nil
	}
	return "", fmt.Errorf("unimplemented")
}

func (c *Connection) mustExecCommand(cmd string, args []string) string {
	res, err := c.execCommand(cmd, args)
	assertEq(err, nil, "unexpected error")
	return res
}

func (d *Database) newConnection() *Connection {
	return &Connection{
		db: d,
		tx: nil,
	}
}

func haveSharedItem(s1 btree.Set[string], s2 btree.Set[string]) bool {
	s1Iter := s1.Iter()
	s2Iter := s2.Iter()
	for ok := s1Iter.First(); ok; ok = s1Iter.Next() {
		s1Key := s1Iter.Key()
		found := s2Iter.Seek(s1Key)
		if found {
			return true
		}
	}

	return false
}

func main() {
	panic("unimplemented")
}
