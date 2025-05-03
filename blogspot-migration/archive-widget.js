function createArchiveWidget() {
    // Archive URLs inlined
    const urls = [];

    // Group posts by year and month
    const archive = {};
    urls.forEach(url => {
        const match = url.match(/\/(\d{4})\/(\d{2})\//);
        if (match) {
            const [_, year, month] = match;
            if (!archive[year]) archive[year] = {};
            if (!archive[year][month]) archive[year][month] = [];
            archive[year][month].push({
                url: url,
                title: decodeURIComponent(url.split('/').pop().replace('.html', '').replace(/-/g, ' '))
            });
        }
    });

    // Create the widget HTML with original Blogger classes
    const widget = document.createElement('div');
    widget.className = 'widget BlogArchive';
    widget.innerHTML = '<h2 class="title">Archive</h2>';

    const content = document.createElement('div');
    content.className = 'widget-content';

    const archiveListDiv = document.createElement('div');
    archiveListDiv.id = 'ArchiveList';

    // Get year and month from the current URL
    const [_, currentYear, currentMonth] = document.location.pathname.match(/\/(\d{4})\/(\d{2})\//);

    // Sort years in descending order
    Object.keys(archive).sort((a, b) => b - a).forEach(year => {
        const yearUl = document.createElement('ul');
        yearUl.className = 'hierarchy';
        
        const yearLi = document.createElement('li');
        yearLi.className = 'archivedate';
        
        // Only expand current year
        if (year === currentYear) {
            yearLi.classList.add('expanded');
        } else {
            yearLi.classList.add('collapsed');
        }
        
        const yearToggle = document.createElement('a');
        yearToggle.className = 'toggle';
        yearToggle.style.paddingRight = '6px'
        yearToggle.innerHTML = `<span class="zippy ${year === currentYear ? 'toggle-open' : 'toggle-closed'}">${year === currentYear ? '▼' : '►'}</span>`;
        yearLi.appendChild(yearToggle);

        const yearLink = document.createElement('a');
        yearLink.className = 'post-count-link';
        yearLink.href = `/${year}/`;
        const yearCount = Object.values(archive[year]).reduce((sum, month) => sum + month.length, 0);
        yearLink.textContent = `${year} (${yearCount})`;
        yearLi.appendChild(yearLink);

        // Sort months in descending order
        Object.keys(archive[year]).sort((a, b) => b - a).forEach(month => {
            const monthUl = document.createElement('ul');
            monthUl.className = 'hierarchy';
            
            const monthLi = document.createElement('li');
            monthLi.className = 'archivedate';
            
            // Only expand current month of current year
            if (year === currentYear && month === currentMonth) {
                monthLi.classList.add('expanded');
            } else {
                monthLi.classList.add('collapsed');
            }

            const monthToggle = document.createElement('a');
            monthToggle.className = 'toggle';
            monthToggle.style.paddingRight = '6px'
            monthToggle.innerHTML = `<span class="zippy ${year === currentYear && month === currentMonth ? 'toggle-open' : 'toggle-closed'}">${year === currentYear && month === currentMonth ? '▼' : '►'}</span>`;
            monthLi.appendChild(monthToggle);

            const monthLink = document.createElement('a');
            monthLink.className = 'post-count-link';
            const monthName = new Date(year, parseInt(month) - 1).toLocaleString('en-US', { month: 'long' });
            monthLink.href = `/${year}/${month}/`;
            monthLink.textContent = `${monthName} (${archive[year][month].length})`;
            monthLi.appendChild(monthLink);

            const postUl = document.createElement('ul');
            postUl.className = 'hierarchy posts';

            archive[year][month].forEach(post => {
                const postLi = document.createElement('li');
                const postLink = document.createElement('a');
                postLink.href = post.url;
                postLink.className = 'post-link';
                postLink.textContent = post.title;
                postLi.appendChild(postLink);
                postUl.appendChild(postLi);
            });

            monthLi.appendChild(postUl);
            monthUl.appendChild(monthLi);
            yearLi.appendChild(monthUl);
        });

        yearUl.appendChild(yearLi);
        archiveListDiv.appendChild(yearUl);
    });

    content.appendChild(archiveListDiv);
    widget.appendChild(content);

    // Add toggle functionality
    widget.addEventListener('click', (e) => {
        if (e.target.classList.contains('zippy')) {
            const li = e.target.closest('li.archivedate');
            if (li) {
                li.classList.toggle('expanded');
                li.classList.toggle('collapsed');
                e.target.classList.toggle('toggle-open');
                e.target.classList.toggle('toggle-closed');
                e.target.textContent = li.classList.contains('expanded') ? '▼' : '►';
            }
        }
    });

    // Replace the existing archive widget
    const existingWidget = document.getElementById('BlogArchive1');
    if (existingWidget) {
        existingWidget.parentNode.replaceChild(widget, existingWidget);
    }
}

// Initialize the widget when the DOM is loaded
document.addEventListener('DOMContentLoaded', createArchiveWidget);