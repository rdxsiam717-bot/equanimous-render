/* Equanimous Voice Studio — shared GitHub API helpers.
 *
 * The site is static (GitHub Pages has no backend). It talks to the GitHub
 * REST API directly from the browser using a fine-grained Personal Access
 * Token (PAT) that the user pastes once. The token is stored ONLY in this
 * browser's localStorage and is sent ONLY to api.github.com. It is never
 * logged and never shown in full — only the last 4 characters.
 *
 * Endpoints used (all documented GitHub REST API):
 *   GET    /repos/{owner}/{repo}/contents/{path}            list a folder / get a file (incl. sha, download_url)
 *   PUT    /repos/{owner}/{repo}/contents/{path}            create or update a file (base64 content; sha needed to update)
 *   DELETE /repos/{owner}/{repo}/contents/{path}            delete a file (needs sha)
 *   POST   /repos/{owner}/{repo}/actions/workflows/{file}/dispatches   start a workflow run
 *   GET    /repos/{owner}/{repo}/actions/workflows/{file}/runs         list workflow runs
 *   GET    /repos/{owner}/{repo}/actions/runs/{run_id}                get one run
 *   GET    /repos/{owner}/{repo}/actions/runs/{run_id}/artifacts      list run artifacts
 *   GET    {artifact.archive_download_url}                  download artifact zip (302 to a pre-signed URL)
 */

var EQ = (function () {
  'use strict';

  var OWNER = 'rdxsiam717-bot';
  var REPO = 'equanimous-render';
  var BRANCH = 'main';
  var WORKFLOW_FILE = 'chatterbox-voiceover.yml';
  var REFERENCES_DIR = 'references';
  var TOKEN_KEY = 'eq_voice_studio_pat_v1';
  var NOTES_KEY = 'eq_voice_notes_v1';
  var API_BASE = 'https://api.github.com';

  /* ---------------- Token ---------------- */

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function setToken(token) {
    token = (token || '').trim();
    if (!token) return false;
    try { localStorage.setItem(TOKEN_KEY, token); } catch (e) { return false; }
    return true;
  }
  function clearToken() {
    try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
  }
  function hasToken() { return !!getToken(); }
  function tokenSuffix() {
    var t = getToken();
    return t ? t.slice(-4) : '';
  }

  /* ---------------- API wrapper ---------------- */

  function api(path, options) {
    options = options || {};
    var token = getToken();
    if (!token) {
      return Promise.reject(new Error('No GitHub token saved. Open the Guide page and paste your token first.'));
    }
    var headers = {
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'Authorization': 'Bearer ' + token
    };
    var fetchOpts = {
      method: options.method || 'GET',
      headers: headers
    };
    if (options.body !== undefined) {
      fetchOpts.body = JSON.stringify(options.body);
      fetchOpts.headers['Content-Type'] = 'application/json';
    }
    return fetch(API_BASE + path, fetchOpts).then(function (resp) {
      if (resp.status === 204) return null;
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var msg = (data && data.message) || ('GitHub API error ' + resp.status);
          var err = new Error(msg);
          err.status = resp.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function repoUrl() { return 'https://github.com/' + OWNER + '/' + REPO; }

  /* ---------------- References (voice clones) ---------------- */

  function listReferences() {
    return api('/repos/' + OWNER + '/' + REPO + '/contents/' + REFERENCES_DIR + '?ref=' + BRANCH)
      .then(function (data) {
        if (!Array.isArray(data)) return [];
        return data
          .filter(function (f) { return f.type === 'file' && /\.(wav|mp3)$/i.test(f.name); })
          .map(function (f) {
            return { name: f.name, path: f.path, sha: f.sha, size: f.size, downloadUrl: f.download_url };
          });
      })
      .catch(function (err) {
        // 404 = the folder does not exist yet (no clones uploaded). Treat as empty.
        if (err.status === 404) return [];
        throw err;
      });
  }

  function getFileSha(path) {
    return api('/repos/' + OWNER + '/' + REPO + '/contents/' + path + '?ref=' + BRANCH)
      .then(function (data) { return data && data.sha ? data.sha : null; })
      .catch(function (err) {
        if (err.status === 404) return null;
        throw err;
      });
  }

  // Upload (create or overwrite) a reference file. Pass sha to overwrite.
  function uploadReference(name, ext, base64, message, sha) {
    var body = {
      message: message || ('Add voice clone ' + name),
      content: base64,
      branch: BRANCH
    };
    if (sha) body.sha = sha;
    return api('/repos/' + OWNER + '/' + REPO + '/contents/' + REFERENCES_DIR + '/' + name + '.' + ext,
      { method: 'PUT', body: body });
  }

  function deleteReference(path, sha) {
    return api('/repos/' + OWNER + '/' + REPO + '/contents/' + path,
      { method: 'DELETE', body: { message: 'Delete voice clone ' + path, sha: sha, branch: BRANCH } });
  }

  /* ---------------- Voiceover workflow ---------------- */

  function dispatchVoiceover(input) {
    return api('/repos/' + OWNER + '/' + REPO + '/actions/workflows/' + WORKFLOW_FILE + '/dispatches',
      {
        method: 'POST',
        body: {
          ref: BRANCH,
          inputs: {
            run_id: input.runId,
            text: input.text,
            reference: input.reference,
            speed: String(input.speed || '0.9'),
            provider: input.provider || 'auto'
          }
        }
      });
  }

  function listWorkflowRuns(perPage) {
    return api('/repos/' + OWNER + '/' + REPO + '/actions/workflows/' + WORKFLOW_FILE +
      '/runs?per_page=' + (perPage || 10));
  }

  function getRun(runId) {
    return api('/repos/' + OWNER + '/' + REPO + '/actions/runs/' + runId);
  }

  function listRunArtifacts(runId) {
    return api('/repos/' + OWNER + '/' + REPO + '/actions/runs/' + runId + '/artifacts');
  }

  // Download an artifact zip. GitHub answers 302 to a pre-signed Azure URL;
  // the browser drops the Authorization header on that cross-origin
  // redirect, which is correct — the Azure URL needs no auth.
  function downloadArtifactZip(archiveUrl) {
    var token = getToken();
    return fetch(archiveUrl, {
      headers: {
        'Accept': 'application/vnd.github+json',
        'Authorization': 'Bearer ' + token
      }
    }).then(function (resp) {
      if (!resp.ok) throw new Error('Artifact download failed (HTTP ' + resp.status + ').');
      return resp.blob();
    });
  }

  function runPageUrl(runId) {
    return repoUrl() + '/actions/runs/' + runId;
  }

  /* ---------------- Small utilities ---------------- */

  // "My Voice 2" -> "my-voice-2". Used for the repo filename.
  function sanitizeName(raw) {
    return (raw || '')
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '')
      .slice(0, 60);
  }

  function readFileAsBase64(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        var result = reader.result || '';
        var comma = result.indexOf(',');
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = function () { reject(new Error('Could not read the file.')); };
      reader.readAsDataURL(file);
    });
  }

  function formatBytes(n) {
    if (n == null || isNaN(n)) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(2) + ' MB';
  }

  function formatDuration(seconds) {
    if (seconds == null || isNaN(seconds)) return '';
    var s = Math.round(seconds);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Default run id, e.g. vo-20261113-0945 (local time).
  function defaultRunId() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return 'vo-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
      '-' + p(d.getHours()) + p(d.getMinutes());
  }

  // Per-clone notes live only in this browser (localStorage), not in the repo.
  function getNote(fileName) {
    try {
      var all = JSON.parse(localStorage.getItem(NOTES_KEY) || '{}');
      return all[fileName] || '';
    } catch (e) { return ''; }
  }
  function setNote(fileName, note) {
    try {
      var all = JSON.parse(localStorage.getItem(NOTES_KEY) || '{}');
      if (note) { all[fileName] = note; } else { delete all[fileName]; }
      localStorage.setItem(NOTES_KEY, JSON.stringify(all));
    } catch (e) { /* ignore */ }
  }

  /* ---------------- Shared page bits ---------------- */

  // Highlight the current page in the nav.
  function initNav() {
    var page = (location.pathname.split('/').pop() || 'index.html').split('?')[0].split('#')[0];
    if (!page) page = 'index.html';
    var links = document.querySelectorAll('.main-nav a');
    links.forEach(function (a) {
      var href = a.getAttribute('href') || '';
      if (href === page || (page === 'index.html' && href === './')) a.classList.add('active');
    });
  }

  // Render the "token saved / not saved" line used on several pages.
  function renderTokenStatus(el) {
    if (!el) return;
    if (hasToken()) {
      el.className = 'status ok';
      el.innerHTML = '<span><span class="dot green"></span><strong>Connected.</strong> ' +
        'A GitHub token is saved in this browser (ends in <code>&hellip;' +
        escapeHtml(tokenSuffix()) + '</code>). It is only sent to api.github.com.</span>';
    } else {
      el.className = 'status warn';
      el.innerHTML = '<span><span class="dot amber"></span><strong>Not connected.</strong> ' +
        'No GitHub token is saved in this browser yet. Open the ' +
        '<a href="guide.html">Guide</a> to create one and paste it here.</span>';
    }
  }

  function showStatus(el, kind, html) {
    if (!el) return;
    el.className = 'status ' + kind;
    el.innerHTML = html;
    el.classList.remove('hidden');
  }
  function hideStatus(el) {
    if (!el) return;
    el.classList.add('hidden');
  }

  return {
    OWNER: OWNER, REPO: REPO, BRANCH: BRANCH, WORKFLOW_FILE: WORKFLOW_FILE,
    REFERENCES_DIR: REFERENCES_DIR, repoUrl: repoUrl,
    getToken: getToken, setToken: setToken, clearToken: clearToken,
    hasToken: hasToken, tokenSuffix: tokenSuffix,
    api: api,
    listReferences: listReferences, getFileSha: getFileSha,
    uploadReference: uploadReference, deleteReference: deleteReference,
    dispatchVoiceover: dispatchVoiceover, listWorkflowRuns: listWorkflowRuns,
    getRun: getRun, listRunArtifacts: listRunArtifacts,
    downloadArtifactZip: downloadArtifactZip, runPageUrl: runPageUrl,
    sanitizeName: sanitizeName, readFileAsBase64: readFileAsBase64,
    formatBytes: formatBytes, formatDuration: formatDuration,
    escapeHtml: escapeHtml, defaultRunId: defaultRunId,
    getNote: getNote, setNote: setNote,
    initNav: initNav, renderTokenStatus: renderTokenStatus,
    showStatus: showStatus, hideStatus: hideStatus
  };
})();
