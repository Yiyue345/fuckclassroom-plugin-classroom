(() => {
  const D = document;
  const pageRuntime = window.FuckClassroomPage?.current?.();
  const pageSignal = pageRuntime?.signal;
  const pageTimeout = pageRuntime?.setTimeout
    ? (callback, delay) => pageRuntime.setTimeout(callback, delay)
    : (callback, delay) => window.setTimeout(callback, delay);
  const pageClearTimeout = pageRuntime?.clearTimeout
    ? (timer) => pageRuntime.clearTimeout(timer)
    : (timer) => window.clearTimeout(timer);
  let pageDisposed = false;

  function disposed() {
    return pageDisposed || Boolean(pageSignal?.aborted);
  }

  function pageFetch(input, init = {}) {
    return window.fetch(input, { ...init, signal: pageSignal });
  }
  const XML = (source) => new DOMParser().parseFromString(source, "application/xml");
  const all = (node, localName) => [...node.getElementsByTagName("*")].filter((item) => item.localName === localName);
  const one = (node, localName) => all(node, localName)[0];
  const num = (value, fallback = 0) => (Number.isFinite(+value) ? +value : fallback);

  const norm = (path) => {
    const output = [];
    for (const part of String(path || "").replace(/^\/+/, "").split("/")) {
      if (!part || part === ".") continue;
      if (part === "..") output.pop();
      else output.push(part);
    }
    return output.join("/");
  };

  const resolve = (base, target) => {
    if (target?.startsWith("/")) return norm(target);
    const parts = norm(base).split("/");
    parts.pop();
    return norm([...parts, ...String(target || "").split("/")].join("/"));
  };

  const relPath = (path) => {
    const parts = norm(path).split("/");
    const name = parts.pop();
    return [...parts, "_rels", `${name}.rels`].join("/");
  };

  class Zip {
    constructor(buffer) {
      this.buffer = buffer;
      this.view = new DataView(buffer);
      this.entries = new Map();
      let position = this.view.byteLength - 22;
      const minimum = Math.max(0, position - 0xffff);
      let endDirectory = -1;
      for (; position >= minimum; position -= 1) {
        if (this.view.getUint32(position, true) === 0x06054b50) {
          endDirectory = position;
          break;
        }
      }
      if (endDirectory < 0) throw new Error("不是有效的 PPTX 文件");

      const count = this.view.getUint16(endDirectory + 10, true);
      let offset = this.view.getUint32(endDirectory + 16, true);
      for (let index = 0; index < count; index += 1) {
        if (this.view.getUint32(offset, true) !== 0x02014b50) throw new Error("PPTX ZIP 目录损坏");
        const method = this.view.getUint16(offset + 10, true);
        const compressedSize = this.view.getUint32(offset + 20, true);
        const nameLength = this.view.getUint16(offset + 28, true);
        const extraLength = this.view.getUint16(offset + 30, true);
        const commentLength = this.view.getUint16(offset + 32, true);
        const localOffset = this.view.getUint32(offset + 42, true);
        const name = norm(new TextDecoder().decode(new Uint8Array(buffer, offset + 46, nameLength)));
        this.entries.set(name, { method, compressedSize, localOffset });
        offset += 46 + nameLength + extraLength + commentLength;
      }
    }

    has(path) {
      return this.entries.has(norm(path));
    }

    async bytes(path) {
      const entry = this.entries.get(norm(path));
      if (!entry) throw new Error(`PPTX 缺少 ${path}`);
      const nameLength = this.view.getUint16(entry.localOffset + 26, true);
      const extraLength = this.view.getUint16(entry.localOffset + 28, true);
      const dataOffset = entry.localOffset + 30 + nameLength + extraLength;
      const data = new Uint8Array(this.buffer, dataOffset, entry.compressedSize);
      if (entry.method === 0) return data.slice();
      if (entry.method !== 8 || !window.DecompressionStream) {
        throw new Error("当前浏览器不支持 PPTX 解压，请下载原文件查看");
      }
      const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
      return new Uint8Array(await new Response(stream).arrayBuffer());
    }

    async text(path) {
      return new TextDecoder().decode(await this.bytes(path));
    }
  }

  async function relationships(zip, source) {
    const path = relPath(source);
    const map = new Map();
    if (!zip.has(path)) return map;
    for (const relationship of all(XML(await zip.text(path)), "Relationship")) {
      if ((relationship.getAttribute("TargetMode") || "").toLowerCase() === "external") continue;
      map.set(relationship.getAttribute("Id"), resolve(source, relationship.getAttribute("Target")));
    }
    return map;
  }

  const relationshipId = (node) => node?.getAttributeNS(
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "id",
  ) || node?.getAttribute("r:id");

  const embedId = (node) => node?.getAttributeNS(
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "embed",
  ) || node?.getAttribute("r:embed");

  function shapeBox(node) {
    const transform = one(node, "xfrm");
    const offset = transform && one(transform, "off");
    const extent = transform && one(transform, "ext");
    if (!offset || !extent) return null;
    const width = num(extent.getAttribute("cx"));
    const height = num(extent.getAttribute("cy"));
    return width && height ? {
      x: num(offset.getAttribute("x")),
      y: num(offset.getAttribute("y")),
      width,
      height,
    } : null;
  }

  function shapeColor(node) {
    const value = one(node, "srgbClr")?.getAttribute("val");
    return value && /^[0-9a-f]{6}$/i.test(value) ? `#${value}` : null;
  }

  function imageMime(path) {
    const extension = (path.split(".").pop() || "").toLowerCase();
    return ({
      png: "image/png",
      jpg: "image/jpeg",
      jpeg: "image/jpeg",
      gif: "image/gif",
      webp: "image/webp",
      svg: "image/svg+xml",
    })[extension] || "application/octet-stream";
  }

  function dataUrl(bytes, type) {
    let binary = "";
    for (let index = 0; index < bytes.length; index += 32768) {
      binary += String.fromCharCode(...bytes.subarray(index, index + 32768));
    }
    return `data:${type};base64,${btoa(binary)}`;
  }

  function drawText(svg, shape, box) {
    const textBody = one(shape, "txBody");
    if (!textBody) return;
    const foreignObject = D.createElementNS("http://www.w3.org/2000/svg", "foreignObject");
    foreignObject.setAttribute("x", box.x);
    foreignObject.setAttribute("y", box.y);
    foreignObject.setAttribute("width", box.width);
    foreignObject.setAttribute("height", box.height);

    const wrapper = D.createElement("div");
    wrapper.setAttribute("xmlns", "http://www.w3.org/1999/xhtml");
    wrapper.className = "ppt-text-box";
    for (const paragraph of all(textBody, "p")) {
      const paragraphElement = D.createElement("p");
      const alignment = one(paragraph, "pPr")?.getAttribute("algn");
      paragraphElement.style.textAlign = alignment === "ctr" ? "center" : alignment === "r" ? "right" : "left";
      const runs = all(paragraph, "r");
      if (runs.length) {
        for (const run of runs) {
          const span = D.createElement("span");
          const runProperties = one(run, "rPr");
          span.textContent = all(run, "t").map((text) => text.textContent || "").join("");
          span.style.fontSize = `${Math.max(900, num(runProperties?.getAttribute("sz"), 1800)) * 127}px`;
          if (runProperties?.getAttribute("b") === "1") span.style.fontWeight = "700";
          if (runProperties?.getAttribute("i") === "1") span.style.fontStyle = "italic";
          const color = runProperties && shapeColor(runProperties);
          if (color) span.style.color = color;
          paragraphElement.append(span);
        }
      } else {
        paragraphElement.textContent = all(paragraph, "t").map((text) => text.textContent || "").join("");
      }
      if (paragraphElement.textContent) wrapper.append(paragraphElement);
    }
    if (wrapper.childNodes.length) {
      foreignObject.append(wrapper);
      svg.append(foreignObject);
    }
  }

  async function renderSlide(zip, path, width, height) {
    const document = XML(await zip.text(path));
    const relationshipMap = await relationships(zip, path);
    const svg = D.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("ppt-slide-svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

    const background = D.createElementNS("http://www.w3.org/2000/svg", "rect");
    background.setAttribute("x", 0);
    background.setAttribute("y", 0);
    background.setAttribute("width", width);
    background.setAttribute("height", height);
    background.setAttribute("fill", "#fff");
    svg.append(background);

    const tree = one(document, "spTree") || document.documentElement;
    for (const node of [...tree.children]) {
      const box = shapeBox(node);
      if (!box) continue;
      if (node.localName === "sp") {
        const color = shapeColor(one(node, "spPr") || node);
        if (color) {
          const rectangle = D.createElementNS("http://www.w3.org/2000/svg", "rect");
          rectangle.setAttribute("x", box.x);
          rectangle.setAttribute("y", box.y);
          rectangle.setAttribute("width", box.width);
          rectangle.setAttribute("height", box.height);
          rectangle.setAttribute("fill", color);
          svg.append(rectangle);
        }
        drawText(svg, node, box);
      } else if (node.localName === "pic") {
        const mediaPath = relationshipMap.get(embedId(one(node, "blip")));
        if (mediaPath && zip.has(mediaPath)) {
          const image = D.createElementNS("http://www.w3.org/2000/svg", "image");
          image.setAttribute("x", box.x);
          image.setAttribute("y", box.y);
          image.setAttribute("width", box.width);
          image.setAttribute("height", box.height);
          image.setAttribute("href", dataUrl(await zip.bytes(mediaPath), imageMime(mediaPath)));
          svg.append(image);
        }
      }
    }
    return svg;
  }

  async function parsePpt(buffer) {
    const zip = new Zip(buffer);
    const presentationPath = "ppt/presentation.xml";
    const document = XML(await zip.text(presentationPath));
    const relationshipMap = await relationships(zip, presentationPath);
    const size = one(document, "sldSz");
    const width = num(size?.getAttribute("cx"), 12192000);
    const height = num(size?.getAttribute("cy"), 6858000);
    let paths = all(document, "sldId").map((node) => relationshipMap.get(relationshipId(node))).filter(Boolean);
    if (!paths.length) {
      paths = [...zip.entries.keys()]
        .filter((path) => /^ppt\/slides\/slide\d+\.xml$/.test(path))
        .sort((left, right) => num(left.match(/slide(\d+)/)?.[1]) - num(right.match(/slide(\d+)/)?.[1]));
    }
    if (!paths.length) throw new Error("课件中没有幻灯片");
    const slides = [];
    for (const path of paths) slides.push(await renderSlide(zip, path, width, height));
    return slides;
  }

  const workspace = D.querySelector("[data-course-media-workspace]");
  if (!workspace) return;

  const rows = [...D.querySelectorAll("[data-lesson-row]")];
  const currentLesson = workspace.querySelector("[data-current-lesson]");
  const video = workspace.querySelector("[data-course-video]");
  const videoEmpty = workspace.querySelector("[data-video-empty]");
  const videoExternal = workspace.querySelector("[data-video-external]");
  const videoTitle = workspace.querySelector("[data-video-title]");
  const liveBadge = workspace.querySelector("[data-live-badge]");
  const pptCard = workspace.querySelector("[data-ppt-card]");
  const pptStage = workspace.querySelector("[data-ppt-stage]");
  const pptStatus = workspace.querySelector("[data-ppt-status]");
  const pptDownload = workspace.querySelector("[data-ppt-download]");
  const pptFullscreen = workspace.querySelector("[data-ppt-fullscreen]");
  const pptCounter = workspace.querySelector("[data-ppt-counter]");
  const pptPrev = workspace.querySelector("[data-ppt-prev]");
  const pptNext = workspace.querySelector("[data-ppt-next]");
  const pptSource = workspace.querySelector("[data-ppt-source]");
  const pptCache = new Map();
  const pptSnapshotCache = new Map();

  let selectedRow = null;
  let pptPages = [];
  let pptIndex = 0;
  let pptLoadToken = 0;
  let livePptTimer = 0;
  let hlsPlayer = null;
  let videoLoadToken = 0;

  function setLinkState(link, href) {
    const enabled = Boolean(href);
    link.href = enabled ? href : "#";
    link.classList.toggle("disabled", !enabled);
    link.setAttribute("aria-disabled", String(!enabled));
    if (enabled) link.removeAttribute("tabindex");
    else link.setAttribute("tabindex", "-1");
  }

  function updatePptControls() {
    const hasSlides = pptPages.length > 0;
    pptCounter.textContent = hasSlides ? `${pptIndex + 1} / ${pptPages.length}` : "0 / 0";
    pptPrev.disabled = !hasSlides || pptIndex === 0;
    pptNext.disabled = !hasSlides || pptIndex === pptPages.length - 1;
  }

  function setPptSource(label, tone = "blue") {
    pptSource.textContent = label;
    pptSource.className = `status ${tone}${label ? "" : " hidden"}`;
    updatePptControls();
  }

  function updateLivePptAspect(page) {
    if (!pptCard.classList.contains("live-mode")) return;
    const image = page?.querySelector("img.live-ppt-image");
    if (!image?.naturalWidth || !image?.naturalHeight) return;
    pptStage.style.aspectRatio = `${image.naturalWidth} / ${image.naturalHeight}`;
  }

  function showPptPage(index) {
    if (!pptPages.length) return;
    pptIndex = Math.max(0, Math.min(index, pptPages.length - 1));
    pptPages.forEach((page, pageIndex) => page.classList.toggle("active", pageIndex === pptIndex));
    updateLivePptAspect(pptPages[pptIndex]);
    updatePptControls();
  }

  function resetPpt(message, state = "empty") {
    pptPages = [];
    pptIndex = 0;
    pptStage.replaceChildren(pptStatus);
    pptStatus.textContent = message;
    pptStatus.className = `ppt-status ${state}`;
    updatePptControls();
  }

  function stopLivePptPolling() {
    if (livePptTimer) pageClearTimeout(livePptTimer);
    livePptTimer = 0;
  }

  function destroyVideoSource() {
    video.pause();
    if (hlsPlayer) hlsPlayer.destroy();
    hlsPlayer = null;
    video.removeAttribute("src");
    video.load();
  }

  function appendSnapshotSlides(slides, followNewest = true) {
    if (!slides.length) return;
    const followLatest = followNewest && (!pptPages.length || pptIndex === pptPages.length - 1);
    pptStatus.classList.add("hidden");
    for (const slide of slides) {
      if (pptPages.some((page) => page.dataset.liveSlideId === String(slide.id))) continue;
      const page = D.createElement("div");
      page.className = "ppt-slide-page live-ppt-slide";
      page.dataset.liveSlideId = String(slide.id);
      const image = D.createElement("img");
      image.className = "live-ppt-image";
      image.alt = `课堂课件第 ${pptPages.length + 1} 页`;
      image.decoding = "async";
      image.width = 1280;
      image.height = 720;
      image.addEventListener("load", () => {
        if (page.classList.contains("active")) updateLivePptAspect(page);
      }, { once: true });
      image.src = slide.image_url;
      page.append(image);
      pptStage.append(page);
      pptPages.push(page);
    }
    pptFullscreen.disabled = !pptPages.length;
    if (followLatest) showPptPage(pptPages.length - 1);
    else if (pptPages.length && !pptPages.some((page) => page.classList.contains("active"))) showPptPage(0);
    else updatePptControls();
  }

  async function fetchPptSnapshots(endpoint) {
    if (!endpoint || disposed()) return [];
    if (pptSnapshotCache.has(endpoint)) return pptSnapshotCache.get(endpoint);
    const response = await pageFetch(endpoint, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (disposed()) return [];
    const slides = Array.isArray(payload.slides) ? payload.slides : [];
    pptSnapshotCache.set(endpoint, slides);
    return slides;
  }

  function loadLivePpt(row, token) {
    const endpoint = row.dataset.pptSlidesUrl || "";
    setLinkState(pptDownload, "");
    pptFullscreen.disabled = true;
    setPptSource("实时更新", "red");
    resetPpt("正在连接直播 PPT…", "loading");
    let afterId = 0;

    const poll = async () => {
      if (disposed() || token !== pptLoadToken || !endpoint) return;
      try {
        const separator = endpoint.includes("?") ? "&" : "?";
        const response = await pageFetch(`${endpoint}${separator}after_id=${afterId}`, { credentials: "same-origin" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (disposed() || token !== pptLoadToken) return;
        const slides = Array.isArray(payload.slides) ? payload.slides : [];
        appendSnapshotSlides(slides);
        afterId = Math.max(afterId, num(payload.last_id), ...slides.map((slide) => num(slide.id)));
        if (!pptPages.length) resetPpt("等待教师推送 PPT…", "loading");
      } catch (error) {
        if (disposed() || error?.name === "AbortError" || token !== pptLoadToken) return;
        if (!pptPages.length) resetPpt(`直播 PPT 暂时不可用：${error.message || error}`, "error");
      }
      if (!disposed() && token === pptLoadToken) livePptTimer = pageTimeout(poll, 3000);
    };
    poll();
  }

  async function loadPpt(row) {
    if (disposed()) return;
    stopLivePptPolling();
    const token = ++pptLoadToken;
    if (row.dataset.live === "true") {
      loadLivePpt(row, token);
      return;
    }
    const url = row.dataset.pptUrl || "";
    const snapshotEndpoint = row.dataset.pptSlidesUrl || "";
    setLinkState(pptDownload, url);
    setPptSource("");
    pptFullscreen.disabled = true;
    resetPpt("正在读取课件…", "loading");
    let exportSlides = [];
    let exportError = null;
    let snapshotSlides = [];
    const snapshotPromise = fetchPptSnapshots(snapshotEndpoint).catch(() => []);

    if (url) {
      try {
        exportSlides = pptCache.get(url) || [];
        if (!exportSlides.length) {
          const response = await pageFetch(url, { credentials: "same-origin", cache: "no-store" });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          exportSlides = await parsePpt(await response.arrayBuffer());
          pptCache.set(url, exportSlides);
        }
      } catch (error) {
        if (disposed() || error?.name === "AbortError") return;
        exportError = error;
      }
    }
    snapshotSlides = await snapshotPromise;
    if (disposed() || token !== pptLoadToken) return;

    if (snapshotSlides.length > exportSlides.length) {
      pptCard.classList.add("live-mode");
      resetPpt("正在打开完整课堂快照…", "loading");
      setPptSource(`完整快照 · ${snapshotSlides.length} 页`);
      appendSnapshotSlides(snapshotSlides, false);
      return;
    }

    if (exportSlides.length) {
      pptCard.classList.remove("live-mode");
      pptStage.style.removeProperty("aspect-ratio");
      setPptSource("录播课件");
      pptStatus.classList.add("hidden");
      pptPages = exportSlides.map((slide) => {
        const page = D.createElement("div");
        page.className = "ppt-slide-page";
        page.append(slide.cloneNode(true));
        pptStage.append(page);
        return page;
      });
      showPptPage(0);
      return;
    }

    if (!url && !snapshotSlides.length) {
      resetPpt("本课次暂无 PPT 课件");
      return;
    }
    resetPpt(`课件读取失败：${exportError?.message || exportError || "没有可用页面"}。`, "error");
  }

  function attachVideoSource(url, autoplay) {
    if (disposed()) return;
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      video.load();
      if (autoplay) video.play().catch(() => {});
      return;
    }
    if (window.Hls?.isSupported()) {
      const player = new window.Hls({ liveSyncDurationCount: 3, maxLiveSyncPlaybackRate: 1.25 });
      hlsPlayer = player;
      player.loadSource(url);
      player.attachMedia(video);
      player.on(window.Hls.Events.MANIFEST_PARSED, () => {
        if (disposed() || player !== hlsPlayer) return;
        if (autoplay) video.play().catch(() => {});
      });
      player.on(window.Hls.Events.ERROR, (_event, data) => {
        if (disposed() || player !== hlsPlayer || !data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) player.startLoad();
        else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) player.recoverMediaError();
        else {
          player.destroy();
          if (player === hlsPlayer) hlsPlayer = null;
          video.classList.add("hidden");
          videoEmpty.classList.remove("hidden");
          videoEmpty.textContent = "直播流连接失败，请稍后重试或在平台中打开";
        }
      });
      return;
    }
    throw new Error("当前浏览器不支持 HLS 直播播放");
  }

  async function loadVideo(row, autoplay = false) {
    if (disposed()) return;
    const token = ++videoLoadToken;
    const url = row.dataset.videoUrl || "";
    destroyVideoSource();
    if (row.dataset.live === "true") {
      video.classList.add("hidden");
      videoEmpty.classList.remove("hidden");
      videoEmpty.textContent = "正在连接直播画面…";
      setLinkState(videoExternal, "");
      try {
        const response = await pageFetch(row.dataset.liveInfoUrl, { credentials: "same-origin" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
        if (disposed() || token !== videoLoadToken) return;
        setLinkState(videoExternal, payload.official_url || "");
        if (!payload.is_live || !payload.playlist_url) throw new Error("直播流暂未就绪");
        videoEmpty.classList.add("hidden");
        video.classList.remove("hidden");
        attachVideoSource(payload.playlist_url, autoplay);
      } catch (error) {
        if (disposed() || error?.name === "AbortError" || token !== videoLoadToken) return;
        video.classList.add("hidden");
        videoEmpty.classList.remove("hidden");
        videoEmpty.textContent = `直播加载失败：${error.message || error}`;
      }
      return;
    }
    if (!url) {
      video.classList.add("hidden");
      videoEmpty.classList.remove("hidden");
      videoEmpty.textContent = "本课次暂无课堂录像";
      setLinkState(videoExternal, "");
      return;
    }

    setLinkState(videoExternal, url);
    videoEmpty.classList.add("hidden");
    video.classList.remove("hidden");
    video.src = url;
    video.load();
    if (autoplay) video.play().catch(() => {});
  }

  function selectLesson(row, options = {}) {
    if (!row || disposed()) return;
    selectedRow = row;
    rows.forEach((candidate) => {
      const active = candidate === row;
      candidate.classList.toggle("selected", active);
      candidate.setAttribute("aria-current", active ? "true" : "false");
    });
    currentLesson.textContent = row.dataset.lessonTitle || "当前课次";
    const isLive = row.dataset.live === "true";
    videoTitle.textContent = isLive ? "课堂直播" : "课堂录像";
    liveBadge.classList.toggle("hidden", !isLive);
    pptCard.classList.toggle("live-mode", isLive);
    if (!isLive) pptStage.style.removeProperty("aspect-ratio");
    loadVideo(row, options.autoplay === true);
    loadPpt(row);

    if (options.focus === "video") {
      workspace.querySelector("[data-video-card]")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else if (options.focus === "ppt") {
      pptCard?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  for (const row of rows) {
    row.addEventListener("click", (event) => {
      const resourceButton = event.target.closest("[data-select-resource]");
      if (resourceButton) {
        event.preventDefault();
        const kind = resourceButton.dataset.selectResource;
        selectLesson(row, { autoplay: kind === "video", focus: kind });
        return;
      }

      const playButton = event.target.closest("[data-lesson-play]");
      if (playButton) {
        event.preventDefault();
        selectLesson(row, { autoplay: true, focus: "video" });
        return;
      }

      if (event.target.closest("a, form, button, input, select, textarea")) return;
      selectLesson(row);
    });
  }

  pptPrev.addEventListener("click", () => showPptPage(pptIndex - 1));
  pptNext.addEventListener("click", () => showPptPage(pptIndex + 1));
  pptFullscreen.addEventListener("click", async () => {
    try {
      if (D.fullscreenElement) await D.exitFullscreen();
      else await pptCard.requestFullscreen();
    } catch (_) {
      // Fullscreen can be unavailable in embedded or restricted browser contexts.
    }
  });

  D.addEventListener("keydown", (event) => {
    if (D.fullscreenElement !== pptCard) return;
    if (event.key === "ArrowLeft") showPptPage(pptIndex - 1);
    if (event.key === "ArrowRight") showPptPage(pptIndex + 1);
  }, { signal: pageSignal });

  const requestedLesson = new URLSearchParams(window.location.search).get("lesson");
  const initialRow = rows.find((row) => row.dataset.lessonId === requestedLesson)
    || rows.find((row) => row.dataset.live === "true")
    || rows.find((row) => row.dataset.videoUrl || row.dataset.pptUrl)
    || rows[0];
  if (initialRow) selectLesson(initialRow);

  function cleanupPage() {
    if (pageDisposed) return;
    pageDisposed = true;
    pptLoadToken += 1;
    videoLoadToken += 1;
    stopLivePptPolling();
    destroyVideoSource();
  }

  pageRuntime?.onDispose?.(cleanupPage);
  window.addEventListener("beforeunload", cleanupPage, { once: true });
})();
