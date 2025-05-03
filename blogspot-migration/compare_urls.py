import json
import os

BASE = input("Enter the base directory: ")

def compare_urls():
    # Load expected URLs
    with open(BASE+'/expected.json', 'r') as f:
        expected_urls = set(json.load(f))
    
    # Load processed URLs
    processed_file = BASE+'/processed_urls.json'
    if not os.path.exists(processed_file):
        print("processed_urls.json not found!")
        return
        
    with open(processed_file, 'r') as f:
        processed_urls = set(json.load(f))
    
    # Find missing URLs
    missing_urls = expected_urls - processed_urls
    
    # Output results
    if missing_urls:
        print(f"\nFound {len(missing_urls)} missing URLs:")
        for url in sorted(missing_urls):
            print(url)
    else:
        print("\nAll expected URLs have been processed!")
        extra_urls = processed_urls - expected_urls
        if extra_urls:
            print(f"\nFound {len(extra_urls)} extra URLs:")
            for url in sorted(extra_urls):
                print(url)
    
    # Additional statistics
    print(f"\nStatistics:")
    print(f"Expected URLs: {len(expected_urls)}")
    print(f"Processed URLs: {len(processed_urls)}")
    print(f"Missing URLs: {len(missing_urls)}")


if __name__ == "__main__":
    compare_urls()