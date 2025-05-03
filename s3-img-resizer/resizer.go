package s3imgresizer

import (
	"image"
	"image/jpeg"
	"image/png"
	"log"
	"os"
	"path/filepath"
	"strings"

	"github.com/nfnt/resize"
)

func ResizeImage(filename string) {
	var img image.Image

	// open "test.jpg"
	file, err := os.Open(filename)
	if err != nil {
		log.Fatal(err)
	}

	switch filepath.Ext(filename) {
	case ".jpg", ".jpeg":
		// decode jpeg into image.Image
		img, err = jpeg.Decode(file)
		if err != nil {
			log.Fatal(err)
		}
	case ".png":
		// decode png into image.Image
		img, err = png.Decode(file)
		if err != nil {
			log.Fatal(err)
		}
	default:
		panic("Unsupported file type")
	}

	file.Close()

	// resize to width 1000 using Lanczos resampling
	// and preserve aspect ratio
	m := resize.Resize(1000, 0, img, resize.Lanczos3)

	out, err := os.Create(strings.TrimSuffix(filepath.Base(filename), filepath.Ext(filename)) + "_resized" + filepath.Ext(filename))
	if err != nil {
		log.Fatal(err)
	}
	defer out.Close()

	// write new image to file
	switch filepath.Ext(filename) {
	case ".jpg", ".jpeg":
		jpeg.Encode(out, m, nil)
	case ".png":
		png.Encode(out, m)
	default:
		panic("Unsupported file type")
	}
}
