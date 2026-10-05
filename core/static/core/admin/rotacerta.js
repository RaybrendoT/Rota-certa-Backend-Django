/* Rota Certa · com o menu lateral recolhido, mostra o nome do item num tooltip à direita
   (o menu não expande no hover, igual ao painel Angular). */
(function () {
  'use strict';

  function isCollapsed() {
    return document.body.classList.contains('sidebar-collapse');
  }

  function setupTooltips() {
    if (!window.bootstrap || !bootstrap.Tooltip) return;
    const links = document.querySelectorAll('.app-sidebar .sidebar-menu .nav-link');
    links.forEach(function (link) {
      const label = (link.querySelector('p') || link).textContent.trim();
      if (!label) return;
      link.setAttribute('aria-label', label);
      bootstrap.Tooltip.getOrCreateInstance(link, { title: label, placement: 'right', trigger: 'hover focus' });
    });
    syncTooltips();
  }

  function syncTooltips() {
    document.querySelectorAll('.app-sidebar .sidebar-menu .nav-link').forEach(function (link) {
      const tip = window.bootstrap && bootstrap.Tooltip.getInstance(link);
      if (!tip) return;
      if (isCollapsed()) {
        tip.enable();
      } else {
        tip.hide();
        tip.disable();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    setupTooltips();
    // O AdminLTE alterna "sidebar-collapse" no body ao clicar no botão de menu.
    new MutationObserver(syncTooltips).observe(document.body, { attributes: true, attributeFilter: ['class'] });
  });
})();
