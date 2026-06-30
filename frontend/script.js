// Base URL of the Flask backend.
//
// Since app.py now serves this frontend itself (at "/"), the API is
// always reachable at the SAME origin the page was loaded from --
// whether that's http://127.0.0.1:5000 locally, or a Railway/Oracle
// public URL once deployed. This means the frontend never needs to be
// edited or reconfigured when moving from local testing to a public
// deployment.
//
// If you ever serve this HTML file from somewhere else entirely (e.g.
// open it directly as a local file with file://, instead of through
// Flask), window.location.origin won't point at the API -- in that
// case, hardcode the real backend URL below instead.
const API_BASE_URL = window.location.origin;

const pdfInput = document.getElementById("pdfInput");
const uploadBtn = document.getElementById("uploadBtn");
const pdfUrlInput = document.getElementById("pdfUrlInput");
const urlBtn = document.getElementById("urlBtn");
const loadingText = document.getElementById("loadingText");
const statusText = document.getElementById("statusText");
const downloadBtn = document.getElementById("downloadBtn");
const downloadRecordsBtn = document.getElementById("downloadRecordsBtn");
const previewBox = document.getElementById("previewBox");
const summaryChips = document.getElementById("summaryChips");
const pageTableWrap = document.getElementById("pageTableWrap");
const pageTableBody = document.getElementById("pageTableBody");
const batchResultsWrap = document.getElementById("batchResultsWrap");
const batchResultsList = document.getElementById("batchResultsList");

// Small label/colour map so the table is easy to scan at a glance.
const TYPE_META = {
  text:  { label: "Text",  className: "chip-text" },
  image: { label: "Image", className: "chip-image" },
  mixed: { label: "Mixed", className: "chip-mixed" },
  empty: { label: "Empty", className: "chip-empty" },
};

function renderSummaryChips(typeCounts) {
  summaryChips.innerHTML = "";
  Object.keys(TYPE_META).forEach((type) => {
    const count = typeCounts[type] || 0;
    const meta = TYPE_META[type];
    const chip = document.createElement("span");
    chip.className = `chip ${meta.className}`;
    chip.textContent = `${meta.label}: ${count}`;
    summaryChips.appendChild(chip);
  });
  summaryChips.classList.remove("hidden");
}

function renderPageTable(pages) {
  pageTableBody.innerHTML = "";
  pages.forEach((p) => {
    const meta = TYPE_META[p.type] || { label: p.type, className: "" };
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${p.page}</td>
      <td><span class="chip ${meta.className}">${meta.label}</span></td>
      <td>${p.has_text ? "✅" : "—"}</td>
      <td>${p.has_image ? "✅" : "—"}</td>
      <td>${p.image_count}</td>
      <td>${p.extraction_method}</td>
      <td>${p.lines_extracted}</td>
    `;
    pageTableBody.appendChild(row);
  });
  pageTableWrap.classList.remove("hidden");
}

function resetResultUI() {
  statusText.textContent = "";
  downloadBtn.classList.add("hidden");
  downloadRecordsBtn.classList.add("hidden");
  previewBox.classList.add("hidden");
  summaryChips.classList.add("hidden");
  pageTableWrap.classList.add("hidden");
  batchResultsWrap.classList.add("hidden");
  batchResultsList.innerHTML = "";
  loadingText.classList.remove("hidden");
}

// Shared by both the file-upload flow and the URL-fetch flow: sends the
// request, then renders the result the same way regardless of source.
async function submitAndRender(formData, triggerButtons) {
  triggerButtons.forEach((btn) => (btn.disabled = true));

  try {
    const response = await fetch(`${API_BASE_URL}/upload`, {
      method: "POST",
      body: formData,
    });

    const result = await response.json();

    if (!response.ok) {
      throw new Error(result.error || "Something went wrong while processing the PDF.");
    }

    const recordNote = result.record_count
      ? `, ${result.record_count} table record(s) parsed`
      : "";
    const speedNote = result.processing_seconds
      ? ` (${result.processing_seconds}s)`
      : "";
    statusText.textContent = `✅ Done! ${result.page_count} page(s), ${result.row_count} row(s) extracted${recordNote}${speedNote}.`;

    if (result.page_type_counts) {
      renderSummaryChips(result.page_type_counts);
    }

    if (result.pages && result.pages.length) {
      renderPageTable(result.pages);
    }

    // Prefer showing the clean structured records in the preview, since
    // that's the directly-usable data; fall back to the flat row view
    // for documents with no detected table (plain text / scanned pages).
    const previewSource = (result.records && result.records.length) ? result.records : result.data;
    previewBox.textContent = JSON.stringify(previewSource.slice(0, 10), null, 2) +
      (previewSource.length > 10 ? "\n... (truncated preview)" : "");
    previewBox.classList.remove("hidden");

    downloadBtn.href = `${API_BASE_URL}/download/${result.json_filename}`;
    downloadBtn.setAttribute("download", result.json_filename);
    downloadBtn.textContent = "⬇ Download Full JSON";
    downloadBtn.classList.remove("hidden");

    if (result.records_filename) {
      downloadRecordsBtn.href = `${API_BASE_URL}/download/${result.records_filename}`;
      downloadRecordsBtn.setAttribute("download", result.records_filename);
      downloadRecordsBtn.classList.remove("hidden");
    }

  } catch (err) {
    statusText.textContent = `❌ Error: ${err.message}`;
  } finally {
    loadingText.classList.add("hidden");
    triggerButtons.forEach((btn) => (btn.disabled = false));
  }
}

// Renders the initial "pending" row for every file in the batch, before
// the request has even been sent -- so the user sees their file list
// immediately rather than staring at a blank loading spinner while a
// potentially slow batch (several PDFs, possibly queued behind a small
// worker pool) processes on the server.
function renderBatchPending(files) {
  batchResultsList.innerHTML = "";
  files.forEach((file) => {
    const li = document.createElement("li");
    li.dataset.filename = file.name;
    li.innerHTML = `
      <span class="batch-file-name">${file.name}</span>
      <span class="batch-file-status pending">Processing…</span>
    `;
    batchResultsList.appendChild(li);
  });
  batchResultsWrap.classList.remove("hidden");
}

// Fills in the final status + a download link for each file once the
// batch response comes back. Matches list items by index against the
// SAME order the files were appended to FormData -- the backend
// guarantees its "results" array comes back in that same input order
// (see /upload-batch in app.py), so this stays correctly aligned even
// when two files in the batch share an identical original filename.
function renderBatchResults(results) {
  const items = batchResultsList.querySelectorAll("li");
  results.forEach((result, index) => {
    const li = items[index];
    if (!li) return;

    if (result.status === "success") {
      const recordNote = result.record_count ? `, ${result.record_count} record(s)` : "";
      li.innerHTML = `
        <span class="batch-file-name">${result.original_filename} — ${result.page_count} page(s)${recordNote}</span>
        <span class="batch-file-status success">✅ Done</span>
      `;
      if (result.json_filename) {
        const link = document.createElement("a");
        link.href = `${API_BASE_URL}/download/${result.json_filename}`;
        link.setAttribute("download", result.json_filename);
        link.textContent = "⬇ JSON";
        link.className = "batch-download-link";
        li.querySelector(".batch-file-status").after(link);
      }
    } else {
      li.innerHTML = `
        <span class="batch-file-name">${result.original_filename}</span>
        <span class="batch-file-status error" title="${result.error || ""}">❌ Failed</span>
      `;
    }
  });
}

async function submitBatchAndRender(files, triggerButtons) {
  triggerButtons.forEach((btn) => (btn.disabled = true));
  renderBatchPending(files);

  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));

  try {
    // Switched from /upload-batch to /upload-batch-job: the old
    // endpoint hard-rejects anything over MAX_FILES_PER_BATCH (20) with
    // an HTTP 400, which is exactly the error a user would see in the
    // browser if they selected more than 20 files at once.
    // /upload-batch-job accepts any number of files and splits them
    // into weight-balanced internal sub-batches on the server side
    // automatically (see backend/batch_balancer.py) -- same response
    // shape (a "results" array in original file order), so nothing
    // else here needs to change.
    const response = await fetch(`${API_BASE_URL}/upload-batch-job`, {
      method: "POST",
      body: formData,
    });

    const result = await response.json();

    if (!response.ok) {
      throw new Error(result.error || "Something went wrong while processing the batch.");
    }

    statusText.textContent = `✅ ${result.message}`;
    renderBatchResults(result.results || []);

  } catch (err) {
    statusText.textContent = `❌ Error: ${err.message}`;
  } finally {
    loadingText.classList.add("hidden");
    triggerButtons.forEach((btn) => (btn.disabled = false));
  }
}

uploadBtn.addEventListener("click", async () => {
  const files = Array.from(pdfInput.files);

  if (files.length === 0) {
    statusText.textContent = "Please select a PDF file first.";
    return;
  }
  const nonPdf = files.find((f) => !f.name.toLowerCase().endsWith(".pdf"));
  if (nonPdf) {
    statusText.textContent = `Only PDF files are supported ("${nonPdf.name}" isn't a PDF).`;
    return;
  }

  resetResultUI();

  if (files.length === 1) {
    // Single file: keep using the original /upload flow unchanged, so
    // existing behavior (download links, page breakdown table, preview)
    // stays exactly as it was for the common case.
    const formData = new FormData();
    formData.append("file", files[0]);
    await submitAndRender(formData, [uploadBtn, urlBtn]);
  } else {
    // Multiple files: use the batch endpoint, which can process several
    // PDFs at once (the server decides how many run truly in parallel
    // based on its own available memory/CPU -- see /api for the live
    // breakdown). Extra files beyond that capacity queue automatically.
    await submitBatchAndRender(files, [uploadBtn, urlBtn]);
  }
});

urlBtn.addEventListener("click", async () => {
  const url = pdfUrlInput.value.trim();

  if (!url) {
    statusText.textContent = "Please paste a direct PDF link first.";
    return;
  }
  if (!/^https?:\/\//i.test(url)) {
    statusText.textContent = "The link must start with http:// or https://";
    return;
  }

  resetResultUI();

  const formData = new FormData();
  formData.append("url", url);
  await submitAndRender(formData, [uploadBtn, urlBtn]);
});
