package s3imgresizer

import (
	"fmt"
	"os"

	"github.com/aws/aws-sdk-go/aws"
	"github.com/aws/aws-sdk-go/aws/credentials"
	"github.com/aws/aws-sdk-go/aws/session"
	"github.com/aws/aws-sdk-go/service/s3"
)

func ListS3FolderContents(bucket, folder string) error {
	// Create a new AWS session
	sess, err := session.NewSession(&aws.Config{
		Credentials:      credentials.NewStaticCredentials(os.Getenv("AWS_ACCESS_KEY_ID"), os.Getenv("AWS_SECRET_ACCESS_KEY"), ""),
		Endpoint:         aws.String(os.Getenv("AWS_ENDPOINT")),
		Region:           aws.String(os.Getenv("AWS_REGION")),
		S3ForcePathStyle: aws.Bool(false),
	})
	if err != nil {
		return fmt.Errorf("failed to create session: %v", err)
	}

	// Create S3 service client
	svc := s3.New(sess)

	// Set up the input parameters
	input := &s3.ListObjectsV2Input{
		Bucket: aws.String(bucket),
		Prefix: aws.String(folder),
	}

	// List objects in the S3 folder
	err = svc.ListObjectsV2Pages(input, func(page *s3.ListObjectsV2Output, lastPage bool) bool {
		for _, item := range page.Contents {
			fmt.Printf("Name: %s, Size: %d, Last Modified: %s\n", *item.Key, *item.Size, *item.LastModified)
		}
		return true
	})

	if err != nil {
		return fmt.Errorf("failed to list objects: %v", err)
	}

	return nil
}
