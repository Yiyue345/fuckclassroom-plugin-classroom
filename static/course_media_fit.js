(() => {
  const pageController = new AbortController();
  const pageSignal = pageController.signal;
  const stage = document.querySelector('[data-ppt-stage]');
  if (!stage) return;

  function cropImageOnlySlide(svg) {
    if (!(svg instanceof SVGElement) || svg.dataset.autoCropped === '1') return;
    svg.dataset.autoCropped = '1';

    const children = [...svg.children];
    const background = children.find((node) => node.localName === 'rect');
    const visible = children.filter((node) => node !== background);
    if (visible.length !== 1 || visible[0].localName !== 'image') return;

    const image = visible[0];
    const viewBox = svg.viewBox?.baseVal;
    const x = Number(image.getAttribute('x'));
    const y = Number(image.getAttribute('y'));
    const width = Number(image.getAttribute('width'));
    const height = Number(image.getAttribute('height'));
    if (!viewBox || !viewBox.width || !viewBox.height || !width || !height) return;

    const areaRatio = (width * height) / (viewBox.width * viewBox.height);
    if (areaRatio < 0.35) return;

    const insideSlide = x >= viewBox.x && y >= viewBox.y
      && x + width <= viewBox.x + viewBox.width
      && y + height <= viewBox.y + viewBox.height;
    if (!insideSlide) return;

    const horizontalMargin = 1 - width / viewBox.width;
    const verticalMargin = 1 - height / viewBox.height;
    if (horizontalMargin < 0.02 && verticalMargin < 0.02) return;

    svg.setAttribute('viewBox', `${x} ${y} ${width} ${height}`);
  }

  function scan(root) {
    if (root instanceof SVGElement && root.classList.contains('ppt-slide-svg')) {
      cropImageOnlySlide(root);
    }
    root.querySelectorAll?.('svg.ppt-slide-svg').forEach(cropImageOnlySlide);
  }

  const cropObserver = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node instanceof Element) scan(node);
      }
    }
  });

  cropObserver.observe(stage, { childList: true, subtree: true });
  scan(stage);

  const card = stage.closest('[data-ppt-card]');
  const prevButton = card?.querySelector('[data-ppt-prev]');
  const nextButton = card?.querySelector('[data-ppt-next]');
  if (!card || !prevButton || !nextButton) return;

  const originalParent = stage.parentElement;
  const readerBody = document.createElement('div');
  readerBody.className = 'ppt-reader-body';

  const thumbnailPanel = document.createElement('aside');
  thumbnailPanel.className = 'ppt-thumbnails-panel';
  thumbnailPanel.setAttribute('aria-label', '幻灯片缩略图导航');

  const thumbnailHeading = document.createElement('div');
  thumbnailHeading.className = 'ppt-thumbnails-heading';
  thumbnailHeading.textContent = '幻灯片';

  const thumbnailList = document.createElement('div');
  thumbnailList.className = 'ppt-thumbnails-list';

  thumbnailPanel.append(thumbnailHeading, thumbnailList);
  originalParent.insertBefore(readerBody, stage);
  readerBody.append(thumbnailPanel, stage);

  let thumbnailButtons = [];
  let rebuildFrame = 0;

  const pages = () => [...stage.querySelectorAll('.ppt-slide-page')];

  function syncActiveThumbnail(scroll = true) {
    const currentPages = pages();
    const activeIndex = currentPages.findIndex((page) => page.classList.contains('active'));

    thumbnailButtons.forEach((button, index) => {
      const active = index === activeIndex;
      button.classList.toggle('active', active);
      button.setAttribute('aria-current', active ? 'page' : 'false');
    });

    if (scroll && activeIndex >= 0 && card.matches(':fullscreen')) {
      thumbnailButtons[activeIndex]?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }

  function navigateTo(index) {
    const currentPages = pages();
    const currentIndex = currentPages.findIndex((page) => page.classList.contains('active'));
    if (currentIndex < 0 || index === currentIndex) return;

    const button = index > currentIndex ? nextButton : prevButton;
    const steps = Math.abs(index - currentIndex);
    for (let step = 0; step < steps; step += 1) button.click();
    syncActiveThumbnail(true);
  }

  function rebuildThumbnails() {
    rebuildFrame = 0;
    const currentPages = pages();
    thumbnailList.replaceChildren();
    thumbnailButtons = [];
    thumbnailHeading.textContent = currentPages.length ? `幻灯片 · ${currentPages.length} 页` : '幻灯片';

    currentPages.forEach((page, index) => {
      const sourceVisual = page.querySelector('svg.ppt-slide-svg, img.live-ppt-image');
      if (!sourceVisual) return;

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'ppt-thumbnail-button';
      button.setAttribute('aria-label', `跳转到第 ${index + 1} 页`);
      button.title = `第 ${index + 1} 页`;

      const preview = sourceVisual.cloneNode(true);
      preview.removeAttribute('id');
      button.append(preview);

      const number = document.createElement('span');
      number.className = 'ppt-thumbnail-number';
      number.textContent = index + 1;
      button.append(number);

      button.addEventListener('click', () => navigateTo(index));
      thumbnailList.append(button);
      thumbnailButtons.push(button);
    });

    syncActiveThumbnail(false);
  }

  function scheduleThumbnailRebuild() {
    if (rebuildFrame) cancelAnimationFrame(rebuildFrame);
    rebuildFrame = requestAnimationFrame(rebuildThumbnails);
  }

  const navigationObserver = new MutationObserver((records) => {
    let needsRebuild = false;
    let needsSync = false;

    for (const record of records) {
      if (record.type === 'childList') needsRebuild = true;
      if (record.type === 'attributes' && record.target instanceof Element
          && record.target.classList.contains('ppt-slide-page')) {
        needsSync = true;
      }
    }

    if (needsRebuild) scheduleThumbnailRebuild();
    else if (needsSync) syncActiveThumbnail(true);
  });

  navigationObserver.observe(stage, {
    childList: true,
    subtree: false,
    attributes: true,
    attributeFilter: ['class'],
  });

  document.addEventListener('fullscreenchange', () => {
    if (document.fullscreenElement === card) {
      syncActiveThumbnail(true);
    }
  }, { signal: pageSignal });

  document.addEventListener('academic:page-before-swap', () => {
    cropObserver.disconnect();
    navigationObserver.disconnect();
    if (rebuildFrame) cancelAnimationFrame(rebuildFrame);
    pageController.abort();
  }, { once: true, signal: pageSignal });

  rebuildThumbnails();

})();
