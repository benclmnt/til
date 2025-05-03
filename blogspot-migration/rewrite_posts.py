# /// script
# dependencies = [
#   "beautifulsoup4",
# ]
# ///

from bs4 import BeautifulSoup
import os

def empty_blog_archive(base_dir):
    # Walk through all directories
    for root, dirs, files in os.walk(base_dir):
        # Skip the images directory
        if 'images' in root:
            continue
            
        # Process HTML files in this directory
        for file in files:
            if file.endswith('.html'):
                file_path = os.path.join(root, file)
                
                # Read the HTML file
                with open(file_path, 'r', encoding='utf-8') as f:
                    soup = BeautifulSoup(f.read(), 'html.parser')
                
                # Find the BlogArchive div
                blog_archive = soup.find('div', id='BlogArchive1')
                if blog_archive:
                    # Empty the content but keep the div
                    blog_archive.clear()
                    
                    # Add the script tag if it doesn't exist
                    script_tag = soup.find('script', src='/scripts/archive-widget.js')
                    if not script_tag:
                        new_script = soup.new_tag('script', src='/scripts/archive-widget.js')
                        # Add it after the BlogArchive div
                        blog_archive.insert_after(new_script)
                
                # Find blog labels div
                blog_labels = soup.find('div', id = 'Label1')
                if blog_labels:
                    blog_labels.clear()

                search = soup.find('div', id = 'HTML2')
                if search:
                    search.clear()

                page_skin = soup.find('style', id = 'page-skin-1')
                if page_skin:
                    new_style = soup.new_tag('link', rel='stylesheet', href='/styles/theme.css', type='text/css')
                    page_skin.replace_with(new_style)
                    
                # Write the modified HTML back to file
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(str(soup))
                    print(f"Updated BlogArchive and added script in: {file_path}")

def main():
    # Get the blog directory from command line or use current directory
    blog_dir = input("Enter the blog directory path (or press Enter to use current directory): ").strip()
    if not blog_dir:
        blog_dir = os.getcwd()
    
    if not os.path.exists(blog_dir):
        print(f"Error: Directory {blog_dir} does not exist")
        return
        
    empty_blog_archive(blog_dir)

if __name__ == "__main__":
    main()