(() => {
  const ui = window.FuckClassroomUI;
  if (!ui) return;
  const { readResponseError, showToast } = ui;
  document.querySelectorAll("form[data-local-file-action]").forEach((form) => {
    const submitButton = form.querySelector("button[type='submit']");
    if (!submitButton) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (form.dataset.running === "true") return;
      const actionLabel = form.dataset.localFileAction || "文件操作";
      const fileName = form.dataset.fileName || "文件";
      form.dataset.running = "true";
      form.setAttribute("aria-busy", "true");
      submitButton.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new URLSearchParams(new FormData(form)),
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(await readResponseError(response, actionLabel + "失败"));
        const payload = await response.json();
        showToast(actionLabel + "成功", payload.message || fileName);
      } catch (error) {
        showToast(
          actionLabel + "未完成",
          error && error.message ? error.message : actionLabel + "失败",
          true
        );
      } finally {
        form.dataset.running = "false";
        form.removeAttribute("aria-busy");
        submitButton.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-download-library]").forEach((library) => {
    const body = library.querySelector("tbody");
    const selectAll = library.querySelector("[data-download-select-all]");
    const deleteButton = document.querySelector("[data-download-delete-selected]");
    const selectionSummary = document.querySelector("[data-download-selection-summary]");
    const fileCount = document.querySelector("[data-download-file-count]");
    if (!body || !selectAll || !deleteButton || !selectionSummary) return;

    const rows = () => Array.from(body.querySelectorAll("[data-download-row]"));
    const selectedRows = () => rows().filter((row) => row.querySelector("[data-download-select]")?.checked);

    function renderDownloadSelection() {
      const allRows = rows();
      const visibleRows = allRows.filter((row) => !row.classList.contains("hidden"));
      const selected = selectedRows();
      const visibleSelectedCount = visibleRows.filter(
        (row) => row.querySelector("[data-download-select]")?.checked
      ).length;

      allRows.forEach((row) => {
        const checkbox = row.querySelector("[data-download-select]");
        row.dataset.downloadSelected = checkbox?.checked ? "true" : "false";
      });
      selectAll.checked = visibleRows.length > 0 && visibleSelectedCount === visibleRows.length;
      selectAll.indeterminate = visibleSelectedCount > 0 && visibleSelectedCount < visibleRows.length;
      selectAll.disabled = visibleRows.length === 0;
      deleteButton.disabled = selected.length === 0 || deleteButton.dataset.running === "true";
      selectionSummary.textContent = selected.length
        ? `已选择 ${selected.length} 个文件`
        : selectionSummary.dataset.defaultLabel || "";
    }

    body.addEventListener("change", (event) => {
      if (event.target.matches("[data-download-select]")) renderDownloadSelection();
    });
    selectAll.addEventListener("change", () => {
      rows().filter((row) => !row.classList.contains("hidden")).forEach((row) => {
        const checkbox = row.querySelector("[data-download-select]");
        if (checkbox) checkbox.checked = selectAll.checked;
      });
      renderDownloadSelection();
    });

    const filterInput = document.querySelector(`[data-filter-input="#${library.id}"]`);
    filterInput?.addEventListener("input", () => window.setTimeout(renderDownloadSelection, 0));

    deleteButton.addEventListener("click", async () => {
      const selected = selectedRows();
      if (!selected.length || deleteButton.dataset.running === "true") return;
      if (!window.confirm(`确认删除所选的 ${selected.length} 个文件？`)) return;

      const requestBody = new URLSearchParams();
      selected.forEach((row) => {
        const checkbox = row.querySelector("[data-download-select]");
        if (checkbox) requestBody.append("relative_path", checkbox.value);
      });
      deleteButton.dataset.running = "true";
      renderDownloadSelection();
      try {
        const response = await fetch(library.dataset.deleteEndpoint || "/downloads/delete-many", {
          method: "POST",
          body: requestBody,
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(await readResponseError(response, "批量删除失败"));
        const payload = await response.json();
        selected.forEach((row) => row.remove());
        const remainingCount = rows().length;
        if (!remainingCount) {
          const emptyRow = document.createElement("tr");
          emptyRow.dataset.downloadEmpty = "";
          emptyRow.innerHTML = '<td class="table-empty" colspan="5">还没有下载文件</td>';
          body.appendChild(emptyRow);
        }
        if (fileCount) fileCount.textContent = `${remainingCount} 个文件`;
        showToast("批量删除完成", payload.message || `已删除 ${selected.length} 个文件`);
      } catch (error) {
        showToast(
          "批量删除未完成",
          error && error.message ? error.message : "批量删除失败",
          true
        );
      } finally {
        deleteButton.dataset.running = "false";
        renderDownloadSelection();
      }
    });

    renderDownloadSelection();
  });

})();
