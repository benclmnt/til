# /// script
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///

import requests
from bs4 import BeautifulSoup
import os
import urllib.parse
import time
import json
import random
import re

class BlogspotScraper:
    def __init__(self, blog_url):
        self.blog_url = blog_url.rstrip('/')
        parsed_url = urllib.parse.urlparse(blog_url)
        self.base_dir = parsed_url.netloc  # Get the first part of the hostname
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        self.processed_urls_file = os.path.join(self.base_dir, 'processed_urls.json')
        self.processed_urls = self.load_processed_urls()
        # Add rate limiting parameters
        self.min_delay = 0.2  # minimum seconds between requests
        self.max_delay = 1  # maximum seconds between requests
        self.width_pattern = re.compile(r'^w\d+(-h\d+)?(-no)?$')

    def load_processed_urls(self):
        if os.path.exists(self.processed_urls_file):
            with open(self.processed_urls_file, 'r') as f:
                return json.load(f)
        return []

    def save_processed_urls(self):
        with open(self.processed_urls_file, 'w') as f:
            json.dump(self.processed_urls, f)
        
    def create_directories(self):
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(os.path.join(self.base_dir, 'images'), exist_ok=True)

    def download_image(self, img_url, post_id):
        if img_url.startswith('http://www.assoc-amazon.com') or img_url.startswith('http://c3.statcounter.com'):
            print(f"skipping assoc-amazon.com image {img_url}")
            return img_url

        try:
            time.sleep(random.uniform(self.min_delay, self.max_delay))
            parsed_url = urllib.parse.urlparse(img_url)
            
            # Handle Google's image optimization paths
            path_parts = parsed_url.path.strip('/').split('/')
            if 'blogger.googleusercontent.com' in parsed_url.netloc:
                if self.width_pattern.match(path_parts[-1]):
                    path_parts.pop()  # Remove the width/height-specific segment
                img_dir = '/'.join(path_parts[:-1]).lstrip('/')
                img_name = path_parts[-1]
                if '.' not in path_parts[-1]:
                    img_name += '.jpg'
            else:
                # For non-blogger URLs, keep original path structure
                img_dir = '/'.join(path_parts[:-1]).lstrip('/')
                img_name = os.path.basename(parsed_url.path)
            
            # Ensure valid filename
            img_name = ''.join(c for c in img_name if c.isalnum() or c in '._-')
            if not img_name:
                print(f"Invalid filename {img_name} from {img_url}, using default name")
                img_name = f"image_{hash(img_url)}.jpg"
            
            full_img_dir = os.path.join(self.base_dir, 'images', img_dir)
            os.makedirs(full_img_dir, exist_ok=True)
            
            img_path = os.path.join(full_img_dir, img_name)
                        # Check if image already exists
            if os.path.exists(img_path):
                print(f"Image already exists: {img_path}")
                return f'/images/{img_dir}/{img_name}'
            
            response = self.session.get(img_url, headers=self.headers)
            if response.status_code == 200:
                with open(img_path, 'wb') as f:
                    f.write(response.content)
                return f'/images/{img_dir}/{img_name}'
            return img_url
        except Exception as e:
            print(f"Error downloading image {img_url}: {e}")
            return img_url

    def process_post(self, post_url):
        if post_url in self.processed_urls:
            print(f"Skipping already processed: {post_url}")
            return

        try:
            # Add delay before fetching each post
            time.sleep(random.uniform(self.min_delay, self.max_delay))
            response = self.session.get(post_url, headers=self.headers)
            if response.status_code != 200:
                print(f"Failed to fetch {post_url}")
                return

            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Process images
            for img in soup.find_all('img'):
                if img.get('src'):
                    src_url = img['src']
                    parsed_url = urllib.parse.urlparse(src_url)
                    
                    if 'blogger.googleusercontent.com' in parsed_url.netloc:
                        path_parts = parsed_url.path.split('/')
                        if path_parts[-1].startswith('w-'):
                            path_parts.pop()
                        img_dir = '/'.join(path_parts[:-1]).lstrip('/')
                        img_name = path_parts[-1]
                        img['src'] = f'/images/{img_dir}/{img_name}'
                    
                    new_src = self.download_image(src_url, os.path.basename(post_url))
                    img['src'] = new_src

            # Create proper URL-based directory structure and save HTML
            parsed_url = urllib.parse.urlparse(post_url)
            post_path = parsed_url.path.lstrip('/')
            if not post_path.endswith('.html'):
                post_path += '.html'
            
            full_post_dir = os.path.join(self.base_dir, os.path.dirname(post_path))
            os.makedirs(full_post_dir, exist_ok=True)
            
            # Save the HTML
            html_path = os.path.join(self.base_dir, post_path)
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(str(soup))

            # Mark as processed after successful completion
            self.processed_urls.append(post_url)
            self.save_processed_urls()

        except Exception as e:
            print(f"Error processing post {post_url}: {e}")

    def scrape(self):
        self.create_directories()
        urls_to_process = {self.blog_url}
        processed_pages = set()
        blog_domain = urllib.parse.urlparse(self.blog_url).netloc

        while urls_to_process:
            current_url = urls_to_process.pop()
            if current_url in processed_pages:
                continue
                
            try:
                # Check if URL belongs to the blog domain
                if urllib.parse.urlparse(current_url).netloc != blog_domain:
                    continue
                    
                time.sleep(random.uniform(self.min_delay, self.max_delay))
                response = self.session.get(current_url, headers=self.headers)
                if response.status_code != 200:
                    continue

                processed_pages.add(current_url)
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Save homepage as index.html on first iteration
                if current_url == self.blog_url:
                    index_path = os.path.join(self.base_dir, 'index.html')
                    with open(index_path, 'w', encoding='utf-8') as f:
                        f.write(str(soup))
                
                # Find all blog post links
                posts = soup.find_all('h3', class_='post-title')
                for post in posts:
                    link = post.find('a')
                    if link and link.get('href'):
                        post_url = link['href']
                        print(f"Found post: {post_url}")
                        if post_url not in self.processed_urls:
                            self.process_post(post_url)
                
                # Find next page and archive links
                for link in soup.find_all('a'):
                    href = link.get('href', '')
                    if (self.blog_url in href and 
                        any(x in href for x in ['/search/label/', '/20', 'blog-pager-older-link']) and href not in self.processed_urls):
                        urls_to_process.add(href)
                        print(f"Added to queue: {href}")

                # Save progress
                self.save_processed_urls()

            except Exception as e:
                print(f"Error processing page {current_url}: {e}")                

def main():
    blog_url = input("Enter the Blogspot URL: ")
    scraper = BlogspotScraper(blog_url)
    scraper.scrape()

if __name__ == "__main__":
    main()
