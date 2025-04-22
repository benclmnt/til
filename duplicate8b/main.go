package main

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/binary"
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/marcboeker/go-duckdb"
	_ "github.com/mattn/go-sqlite3"
	"golang.org/x/crypto/blake2b"
)

const DATA_FOLDER = "data"

var SQLITE_BUFFER_SIZE int

func main() {
	now := time.Now()
	var N uint64
	var workload string
	flag.Uint64Var(&N, "N", 0, "N")
	flag.IntVar(&SQLITE_BUFFER_SIZE, "sqlite_buffer_size", 50, "sqlite buffer size")
	flag.StringVar(&workload, "workload", "", "workload to run")
	flag.Parse()
	// use primary key
	// 4.46s for 1 million records
	// 1m59.79s for 10 million records

	// not use primary key
	// 1.124s for inserting + creating index
	// 8.3s for inserting only, 12s +creating index.
	// f, _ := os.Create("cpuprof.prof")
	// defer f.Close()

	db := prepareSqlite()
	defer db.Close()

	// pprof.StartCPUProfile(f)
	if workload == "generateOnly" {
		generateNumbers(N)
	} else if workload == "sqlite" {
		executeSqlite(db, N)
	} else if workload == "sqlite2" {
		executeSqlite2(db, N)
	} else if workload == "sqlite3" {
		executeSqlite3(db, N)
	} else if workload == "duckdb2" {
		executeDuckDB2(N)
	} else if workload == "duckdb" {
		executeDuckDB(N)
	} else {
		panic(workload)
	}
	// pprof.StopCPUProfile()

	fmt.Println(time.Since(now))
}

func buildInParallel(fn func(idx int), printProgress int) {
	now := time.Now()
	var wg sync.WaitGroup
	for j := 1; j < 10; j++ {
		j := j
		wg.Add(1)
		go func() {
			for i := j * 1000; i < (j+1)*1000; i++ {
				i := i
				fn(i)
				if i%printProgress == 0 {
					fmt.Print("\033[G\033[K")
					fmt.Printf("[%+v] Built %d\n", time.Since(now).Seconds(), i)
					fmt.Print("\033[A")
				}
			}
			wg.Done()
		}()
	}

	wg.Wait()
}

// executeSqlite maintains an index from the beginning
func executeSqlite(db *sql.DB, end uint64) {
	// use primary key here
	sqlStmt := `
	create table foo (id integer not null PRIMARY KEY);
	delete from foo;
	`

	_, err := db.Exec(sqlStmt)
	if err != nil {
		fmt.Printf("%q: %s\n", err, sqlStmt)
		return
	}

	batchInsertStmt := "INSERT INTO foo VALUES " + strings.Join(repeatedSlice("(?)", SQLITE_BUFFER_SIZE), ", ")
	buffer := make([]interface{}, SQLITE_BUFFER_SIZE)

	for i := 0; i < int(end); i++ {
		num := generateRandomNumber(uint64(i))
		buffer[i%SQLITE_BUFFER_SIZE] = num
		if i%SQLITE_BUFFER_SIZE == SQLITE_BUFFER_SIZE-1 {
			_, err := db.Exec(batchInsertStmt, buffer...)
			if err != nil {
				fmt.Printf("Error: %v %d\n", err, i)
				panic(err)
			}
		}
	}
}

// executeSqlite2 first inserts then creates index
func executeSqlite2(db *sql.DB, end uint64) {

	sqlStmt := `
	create table foo (id integer not null);
	delete from foo;
	`

	_, err := db.Exec(sqlStmt)
	if err != nil {
		fmt.Printf("%q: %s\n", err, sqlStmt)
		return
	}

	batchInsertStmt := "INSERT INTO foo VALUES " + strings.Join(repeatedSlice("(?)", SQLITE_BUFFER_SIZE), ", ")
	buffer := make([]interface{}, SQLITE_BUFFER_SIZE)

	for i := 0; i < int(end); i++ {
		num := generateRandomNumber(uint64(i))
		buffer[i%SQLITE_BUFFER_SIZE] = num
		if i%SQLITE_BUFFER_SIZE == SQLITE_BUFFER_SIZE-1 {
			_, err := db.Exec(batchInsertStmt, buffer...)
			if err != nil {
				fmt.Printf("Error: %v %d\n", err, i)
				log.Fatal(err)
			}
		}
	}

	_, err = db.Exec("create unique index if not exists idx_id on foo(id)")
	if err != nil {
		panic(err)
	}
}

// executeSqlite3 first inserts via prepared statements then creates index
func executeSqlite3(db *sql.DB, end uint64) {
	now := time.Now()

	sqlStmt := `
	create table foo (id integer not null);
	delete from foo;
	`

	_, err := db.Exec(sqlStmt)
	if err != nil {
		fmt.Printf("%q: %s\n", err, sqlStmt)
		return
	}

	batchInsertStmt := "INSERT INTO foo VALUES " + strings.Join(repeatedSlice("(?)", SQLITE_BUFFER_SIZE), ", ")
	preparedStmt, err := db.Prepare(batchInsertStmt)
	if err != nil {
		panic(err)
	}
	buffer := make([]interface{}, SQLITE_BUFFER_SIZE)

	for i := 0; i < int(end); i++ {
		num := generateRandomNumber(uint64(i))
		buffer[i%SQLITE_BUFFER_SIZE] = num
		if i%SQLITE_BUFFER_SIZE == SQLITE_BUFFER_SIZE-1 {
			_, err := preparedStmt.Exec(buffer...)
			if err != nil {
				fmt.Printf("Error: %v %d\n", err, i)
				log.Fatal(err)
			}
		}
	}

	fmt.Println(time.Since(now))

	_, err = db.Exec("create unique index if not exists idx_id on foo(id)")
	if err != nil {
		panic(err)
	}
}

func prepareSqlite() *sql.DB {
	// os.Remove("./foo.db")

	db, err := sql.Open("sqlite3", ":memory:")
	if err != nil {
		log.Fatal(err)
	}
	db.Exec("PRAGMA journal_mode = OFF;")
	db.Exec("PRAGMA synchronous = 0;")
	// db.Exec("PRAGMA cache_size = 1000000;")
	db.Exec("PRAGMA locking_mode = EXCLUSIVE;")
	// db.Exec("PRAGMA temp_store = MEMORY;")
	return db
}

// use the appender api
func executeDuckDB(end uint64) {
	os.Remove("./duckdb.db")

	connector, err := duckdb.NewConnector("./duckdb.db", func(execer driver.ExecerContext) error {
		_, err := execer.ExecContext(context.Background(), "CREATE TABLE foo (id BIGINT NOT NULL PRIMARY KEY);", nil)
		if err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		panic(err)
	}
	conn, err := connector.Connect(context.Background())
	if err != nil {
		panic(err)
	}
	defer conn.Close()

	appender, err := duckdb.NewAppenderFromConn(conn, "", "foo")
	if err != nil {
		panic(err)
	}
	defer appender.Close()

	for i := 0; i < int(end); i++ {
		num := generateRandomNumber(uint64(i))
		err := appender.AppendRow(num)
		if err != nil {
			fmt.Printf("Error: %v %d", err, i)
			log.Fatal(err)
		}
	}

	err = appender.Flush()
	if err != nil {
		fmt.Printf("Error: %v", err)
		log.Fatal(err)
	}
}

func executeDuckDB2(end uint64) {
	os.Remove("./duckdb.db")

	connector, err := duckdb.NewConnector("./duckdb.db", func(execer driver.ExecerContext) error {
		_, err := execer.ExecContext(context.Background(), "CREATE TABLE foo (id BIGINT NOT NULL);", nil)
		if err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		panic(err)
	}
	conn, err := connector.Connect(context.Background())
	if err != nil {
		panic(err)
	}
	defer conn.Close()

	appender, err := duckdb.NewAppenderFromConn(conn, "", "foo")
	if err != nil {
		panic(err)
	}
	defer appender.Close()

	for i := 0; i < int(end); i++ {
		num := generateRandomNumber(uint64(i))
		err := appender.AppendRow(num)
		if err != nil {
			fmt.Printf("Error: %v %d", err, i)
			log.Fatal(err)
		}
	}

	err = appender.Flush()
	if err != nil {
		fmt.Printf("Error: %v", err)
		log.Fatal(err)
	}

	db := sql.OpenDB(connector)

	_, err = db.Exec("create unique index idx_id on foo(id)")
	if err != nil {
		panic(err)
	}
}

func generateNumbers(N uint64) {
	for i := uint64(0); i < N; i++ {
		j := generateRandomNumber(i)
		_ = j
	}
}

var randBytes = make([]byte, 8)

func generateRandomNumber(i uint64) uint64 {
	binary.BigEndian.PutUint64(randBytes, i)
	j := process(blake2b.Sum256(randBytes))
	if j < 1e18 {
		j |= 1 << 60
	}
	return j
}

func process(in [32]byte) uint64 {
	_ = in[31] // bounds check for compiler
	in[0] = in[0] ^ in[8] ^ in[16] ^ in[24]
	in[1] = in[1] ^ in[9] ^ in[17] ^ in[25]
	in[2] = in[2] ^ in[10] ^ in[18] ^ in[26]
	in[3] = in[3] ^ in[11] ^ in[19] ^ in[27]
	in[4] = in[4] ^ in[12] ^ in[20] ^ in[28]
	in[5] = in[5] ^ in[13] ^ in[21] ^ in[29]
	in[6] = in[6] ^ in[14] ^ in[22] ^ in[30]
	in[7] = in[7] ^ in[15] ^ in[23] ^ in[31]
	return binary.BigEndian.Uint64(in[:8]) & uint64(math.MaxInt64)
}

// ---
// helper functions
// ---
func repeatedSlice(value string, n int) []string {
	arr := make([]string, n)
	for i := 0; i < n; i++ {
		arr[i] = value
	}
	return arr
}
