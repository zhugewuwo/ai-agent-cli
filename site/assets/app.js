// TOC scroll spy + collapse toggle
(function () {
  var toc = document.querySelector('.toc');
  if (!toc) return;
  var links = Array.prototype.slice.call(toc.querySelectorAll('a[href^="#"]'));
  if (!links.length) return;

  var map = {};
  var targets = [];
  links.forEach(function (a) {
    var id = decodeURIComponent(a.getAttribute('href').slice(1));
    var el = document.getElementById(id);
    if (el) { map[id] = a; targets.push(el); }
  });

  function onScroll() {
    var pos = window.scrollY + 120;
    var current = targets[0];
    for (var i = 0; i < targets.length; i++) {
      if (targets[i].offsetTop <= pos) current = targets[i];
    }
    links.forEach(function (a) { a.parentElement.classList.remove('active'); });
    if (current && map[current.id]) map[current.id].parentElement.classList.add('active');
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  var toggle = toc.querySelector('.toc-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var ul = toc.querySelector('ul');
      var hidden = ul.style.display === 'none';
      ul.style.display = hidden ? '' : 'none';
      toggle.textContent = hidden ? '\u226B' : '\u226A';
    });
  }
})();
