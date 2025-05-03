import os
from collections import defaultdict

def count_html_files(base_dir):
    html_counts = defaultdict(int)
    total_count = 0
    
    # Walk through all directories
    for root, dirs, files in os.walk(base_dir):
        # Skip the images directory
        if 'images' in root:
            continue
            
        # Count HTML files in this directory
        html_files = [f for f in files if f.endswith('.html')]
        if html_files:
            # Get relative path from base_dir
            rel_path = os.path.relpath(root, base_dir)
            if rel_path == '.':
                rel_path = 'root'
            html_counts[rel_path] = len(html_files)
            total_count += len(html_files)
    
    # Print results
    print(f"\nHTML File Count by Directory in {base_dir}:")
    print("-" * 50)
    for directory, count in sorted(html_counts.items()):
        print(f"{directory}: {count} files")
    print("-" * 50)
    print(f"Total HTML files: {total_count}")

def main():
    # Get the blog directory from command line or use current directory
    blog_dir = input("Enter the blog directory path (or press Enter to use current directory): ").strip()
    if not blog_dir:
        blog_dir = os.getcwd()
    
    if not os.path.exists(blog_dir):
        print(f"Error: Directory {blog_dir} does not exist")
        return
        
    count_html_files(blog_dir)

if __name__ == "__main__":
    main()