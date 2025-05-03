package main

import (
	"os"

	s3imgresizer "github.com/benclmnt/s3-img-resizer"
)

func main() {
	// bucket := os.Getenv("AWS_BUCKET")
	// folder := ""
	file := os.Args[1]

	// err := s3imgresizer.ListS3FolderContents(bucket, folder)
	// if err != nil {
	// 	log.Fatalf("Error: %v", err)
	// }
	s3imgresizer.ResizeImage(file)
}
