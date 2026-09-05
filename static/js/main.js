// Mobile nav toggle
const menuToggle = document.querySelector('.menu-toggle');
const mainNav = document.getElementById('mainNav');
if (menuToggle && mainNav) {
  menuToggle.addEventListener('click', () => mainNav.classList.toggle('open'));
}

// Quantity adjuster on product page
function adjustQty(delta) {
  const input = document.getElementById('qtyInput');
  if (!input) return;
  const max = parseInt(input.max || 999);
  let val = parseInt(input.value) + delta;
  if (val < 1) val = 1;
  if (val > max) val = max;
  input.value = val;
}

// Auto-dismiss flash messages
document.querySelectorAll('.flash').forEach(el => {
  setTimeout(() => {
    el.style.transition = 'opacity .4s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 400);
  }, 4000);
});

// Lazy-load images
if ('IntersectionObserver' in window) {
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        e.target.loading = 'eager';
        io.unobserve(e.target);
      }
    });
  });
  document.querySelectorAll('img[loading="lazy"]').forEach(img => io.observe(img));
}