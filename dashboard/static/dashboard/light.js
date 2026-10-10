// The dashboard is light only (UNFOLD THEME = "light"), but a browser that toggled
// dark mode before keeps "dark" in localStorage and gets a half-dark page. Clear it.
(() => {
  try { localStorage.setItem("adminTheme", '"light"'); } catch (e) { /* storage blocked: the class fix below still runs */ }
  const root = document.documentElement;
  const fix = () => { if (root.classList.contains("dark")) root.classList.remove("dark"); };   // only on change, or the observer loops
  fix();
  new MutationObserver(fix).observe(root, { attributes: true, attributeFilter: ["class"] });
})();
