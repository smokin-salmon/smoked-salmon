function initTheme() {
    const themeToggle = document.getElementById('theme-toggle');
    const storedTheme = localStorage.getItem('theme') || 'light';
    
    // Set initial theme
    document.documentElement.setAttribute('data-theme', storedTheme);
    
    // Update button text based on theme
    themeToggle.innerHTML = storedTheme === 'light' ? '🌙' : '☀️';
    
    themeToggle.addEventListener('click', () => {
        const currentTheme = document.documentElement.getAttribute('data-theme');
        const newTheme = currentTheme === 'light' ? 'dark' : 'light';
        
        // Update theme
        document.documentElement.setAttribute('data-theme', newTheme);
        localStorage.setItem('theme', newTheme);
        
        // Update button text
        themeToggle.innerHTML = newTheme === 'light' ? '🌙' : '☀️';
    });
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', initTheme);
