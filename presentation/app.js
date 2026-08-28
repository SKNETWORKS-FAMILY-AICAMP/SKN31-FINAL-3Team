const slides = [...document.querySelectorAll('.slide')];
const dots = [...document.querySelectorAll('.rail-dot')];
const currentNo = document.getElementById('currentNo');
const progressBar = document.getElementById('progressBar');
let current = 0;

function goTo(index) {
  current = Math.max(0, Math.min(slides.length - 1, index));
  slides[current].scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function update(index) {
  current = index;
  currentNo.textContent = String(index + 1).padStart(2, '0');
  progressBar.style.width = `${((index + 1) / slides.length) * 100}%`;
  dots.forEach((dot, i) => dot.classList.toggle('active', i === index));
  document.title = `${String(index + 1).padStart(2, '0')} · ${slides[index].dataset.title} | bidding flow`;
}

const observer = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting && entry.intersectionRatio >= 0.55) update(slides.indexOf(entry.target));
  });
}, { threshold: [0.55] });
slides.forEach((slide) => observer.observe(slide));

document.getElementById('prevBtn').addEventListener('click', () => goTo(current - 1));
document.getElementById('nextBtn').addEventListener('click', () => goTo(current + 1));
document.getElementById('fullBtn').addEventListener('click', () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
  else document.exitFullscreen?.();
});

document.addEventListener('keydown', (event) => {
  if (['ArrowRight', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); goTo(current + 1); }
  if (['ArrowLeft', 'PageUp'].includes(event.key)) { event.preventDefault(); goTo(current - 1); }
  if (event.key === 'Home') { event.preventDefault(); goTo(0); }
  if (event.key === 'End') { event.preventDefault(); goTo(slides.length - 1); }
  if (event.key.toLowerCase() === 'f') document.getElementById('fullBtn').click();
});

const initialIndex = slides.findIndex((slide) => `#${slide.id}` === window.location.hash);
if (initialIndex >= 0) {
  requestAnimationFrame(() => {
    slides[initialIndex].scrollIntoView({ behavior: 'auto', block: 'start' });
    update(initialIndex);
  });
} else {
  update(0);
}
