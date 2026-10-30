(() => {
  const skip = (button) => button.matches("[data-no-loader], .filter-tab, .modal-close, .icon-button") || button.disabled;

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || skip(button)) return;
    button.classList.remove("button-pop");
    void button.offsetWidth;
    button.classList.add("button-pop");
    if (button.dataset.loading === "false") return;
    button.classList.add("is-loading");
    button.setAttribute("aria-busy", "true");
    window.setTimeout(() => {
      button.classList.remove("is-loading");
      button.removeAttribute("aria-busy");
    }, 1400);
  });

  document.addEventListener("animationend", (event) => {
    if (event.animationName === "button-pop") event.target.classList.remove("button-pop");
  });

  document.querySelectorAll("img").forEach((image) => image.addEventListener("load", () => image.classList.add("is-loaded"), { once: true }));
})();
