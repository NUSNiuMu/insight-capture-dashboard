// Older mobile browsers can pan the document through a native modal backdrop.
// Keep native scrolling inside the modal; never change html/body overflow.
export function guardModalScroll(modal, { fitViewport = false } = {}) {
  const doc = modal.ownerDocument;
  const releaseViewport = fitViewport ? fitModalViewport(modal) : null;
  let drag = null;
  let disposed = false;

  function start(event) {
    drag = null;
    if (!modal.open || event.touches.length !== 1 || !modal.contains(event.target)) return;
    const touch = event.touches[0], bounds = modal.getBoundingClientRect();
    drag = { x: touch.clientX, y: touch.clientY, target: event.target,
      inside: touch.clientX >= bounds.left && touch.clientX <= bounds.right &&
              touch.clientY >= bounds.top && touch.clientY <= bounds.bottom };
  }

  function move(event) {
    if (!modal.open || !drag || event.touches.length !== 1) return;
    const touch = event.touches[0], dx = touch.clientX - drag.x, dy = touch.clientY - drag.y;
    drag.x = touch.clientX; drag.y = touch.clientY;
    if (event.defaultPrevented || !event.cancelable || (!dx && !dy)) return;
    if (drag.inside) {
      const field = drag.target.closest('input,textarea');
      const selection = doc.getSelection();
      // Leave native text selection and caret handles to the browser.
      if ((field === doc.activeElement && typeof field?.selectionStart === 'number' &&
           field.selectionStart !== field.selectionEnd) ||
          (selection && !selection.isCollapsed && modal.contains(selection.anchorNode))) return;
      const horizontal = Math.abs(dx) > Math.abs(dy), delta = horizontal ? dx : dy;
      for (let node = drag.target; node; node = node.parentElement) {
        const style = doc.defaultView.getComputedStyle(node);
        const policy = horizontal ? style.overflowX : style.overflowY;
        const offset = horizontal ? node.scrollLeft : node.scrollTop;
        const size = horizontal ? node.clientWidth : node.clientHeight;
        const extent = horizontal ? node.scrollWidth : node.scrollHeight;
        if ((policy === 'auto' || policy === 'scroll' || (horizontal && node.tagName === 'INPUT')) &&
            extent > size && (delta > 0 ? offset > 0 : offset + size < extent)) return;
        if (node === modal) break;
      }
    }
    event.preventDefault();
  }

  function end() { drag = null; }
  function closed() { if (!modal.open) dispose(); }
  function dispose() {
    if (disposed) return;
    disposed = true; drag = null;
    releaseViewport?.();
    doc.removeEventListener('touchstart', start);
    doc.removeEventListener('touchmove', move);
    doc.removeEventListener('touchend', end);
    doc.removeEventListener('touchcancel', end);
    modal.removeEventListener('close', closed);
  }

  // Backdrop gestures are not reliably cancelable on the dialog element itself.
  // These listeners exist only while this modal is open and are removed on close.
  doc.addEventListener('touchstart', start, { passive: false });
  doc.addEventListener('touchmove', move, { passive: false });
  doc.addEventListener('touchend', end, { passive: true });
  doc.addEventListener('touchcancel', end, { passive: true });
  modal.addEventListener('close', closed);
  return dispose;
}

// A software keyboard can shrink only the visual viewport, leaving dvh unchanged.
// Fit the form to that visible rectangle without moving or locking the page.
function fitModalViewport(modal) {
  const view = modal.ownerDocument.defaultView, viewport = view?.visualViewport;
  if (!viewport) return null;
  const names = ['--modal-viewport-inset', '--modal-viewport-width', '--modal-viewport-height'];
  const saved = names.map(name => [name, modal.style.getPropertyValue(name), modal.style.getPropertyPriority(name)]);
  const hadStyle = modal.hasAttribute('style');
  let frame = 0;
  function update() {
    frame = 0;
    if (!modal.open || viewport.width <= 0 || viewport.height <= 0) return;
    const right = Math.max(0, view.innerWidth - viewport.offsetLeft - viewport.width);
    const bottom = Math.max(0, view.innerHeight - viewport.offsetTop - viewport.height);
    modal.style.setProperty(names[0], `${viewport.offsetTop}px ${right}px ${bottom}px ${viewport.offsetLeft}px`);
    modal.style.setProperty(names[1], `${viewport.width}px`);
    modal.style.setProperty(names[2], `${viewport.height}px`);
  }
  function schedule() { if (!frame) frame = view.requestAnimationFrame(update); }
  viewport.addEventListener('resize', schedule);
  viewport.addEventListener('scroll', schedule);
  update();
  return () => {
    view.cancelAnimationFrame(frame);
    viewport.removeEventListener('resize', schedule);
    viewport.removeEventListener('scroll', schedule);
    for (const [name, value, priority] of saved) {
      if (value) modal.style.setProperty(name, value, priority);
      else modal.style.removeProperty(name);
    }
    if (!hadStyle && !modal.style.length) modal.removeAttribute('style');
  };
}
