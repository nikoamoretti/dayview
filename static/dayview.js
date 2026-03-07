    // ── State ──────────────────────────────────────────────────────────
    let currentDate    = null;   // "2024-02-23"
    let availableDays  = [];
    let currentTab     = 'daily';
    let overviewLoaded = false;
    let projectsLoaded = false;
    let projectsData   = [];
    let currentFilter  = null;
    let currentProjectDetail = null;
    let projectsSearch = '';
    let projectsSort   = 'updated';  // 'updated' | 'name' | 'entries'
    let pollTimer      = null;   // interval id for content polling
    let pollStart      = null;   // timestamp when polling began
    let roleFilter     = null;   // active role filter (null = show all)
    let lastRolesData  = null;   // cached roles array for filtering
    let lastTimeline   = null;   // cached timeline for re-rendering

    const TAB_TITLES = {
      overview: 'Shipped Feed',
      projects: 'Project Radar',
      database: 'Activity Map',
      daily: 'Daily Journal',
    };

    // ── Helpers ────────────────────────────────────────────────────────

    /** Format ISO timestamp to local HH:MM */
    function fmtTime(ts) {
      if (!ts) return '—';
      try {
        return new Date(ts).toLocaleTimeString('en-US', {
          hour: '2-digit', minute: '2-digit', hour12: false
        });
      } catch { return ts; }
    }

    /** Format a date string to human-readable, e.g. "Monday, February 23, 2026" */
    function fmtDateLong(dateStr) {
      if (!dateStr) return '—';
      try {
        // Parse as local date to avoid timezone-shift to previous day
        const [y, m, d] = dateStr.split('-').map(Number);
        const dt = new Date(y, m - 1, d);
        return dt.toLocaleDateString('en-US', {
          weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
        });
      } catch { return dateStr; }
    }

    /** Escape HTML special chars */
    function esc(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    /** Validate a hex color string to prevent style injection */
    function safeColor(c) {
      return /^#[0-9A-Fa-f]{3,8}$/.test(c) ? c : '#64748B';
    }

    /** Deterministic pastel colour from app name first letter */
    function appColor(name) {
      const palette = [
        '#4A9EFF','#7C6FFF','#FF6B6B','#4ADE80',
        '#FBBF24','#F472B6','#34D399','#60A5FA',
        '#A78BFA','#FB923C',
      ];
      const code = (name || 'A').toUpperCase().charCodeAt(0);
      return palette[code % palette.length];
    }

    /** Highlight keyword occurrences in a text string */
    function highlightKeyword(text, keyword) {
      if (!keyword) return esc(text);
      const safe = keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const re   = new RegExp(`(${safe})`, 'gi');
      return esc(text).replace(re, '<mark>$1</mark>');
    }

    // ── UI Update helpers ──────────────────────────────────────────────

    function setSkeletonLoading(elId) {
      const el = document.getElementById(elId);
      if (!el) return;
      el.innerHTML = `
        <div class="skeleton-block" aria-busy="true">
          <div class="skeleton-line" style="width:88%"></div>
          <div class="skeleton-line" style="width:72%"></div>
          <div class="skeleton-line" style="width:80%"></div>
        </div>`;
    }

    function setError(elId, message) {
      const el = document.getElementById(elId);
      if (el) el.innerHTML = `<div class="error-state" role="alert">&#9888;&#xFE0F; ${esc(message)}</div>`;
    }

    function setEmpty(elId, message) {
      const el = document.getElementById(elId);
      if (el) el.innerHTML = `<div class="empty-state">${esc(message)}</div>`;
    }

    // ── Date Navigation ────────────────────────────────────────────────

    function renderDayStrip() {
      const strip = document.getElementById('day-strip');
      if (!strip) return;

      // Show newest first so today is on the left
      const days = [...availableDays].sort().reverse();
      strip.innerHTML = days.map(d => {
        const dt = new Date(d + 'T12:00:00');
        const label = dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
        const cls = d === currentDate ? 'day-chip active' : 'day-chip';
        return `<button class="${cls}" data-date="${d}">${label}</button>`;
      }).join('');

      strip.querySelectorAll('.day-chip').forEach(btn => {
        btn.addEventListener('click', () => loadDay(btn.dataset.date));
      });
    }

    function updateDayStripActive() {
      document.querySelectorAll('.day-chip').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.date === currentDate);
      });
    }

    function updateDateDisplay() {
      const display = document.getElementById('date-display');
      const picker  = document.getElementById('date-picker');
      if (display) display.textContent = fmtDateLong(currentDate);
      if (picker)  picker.value = currentDate || '';
      updateNavButtons();
      updateDayStripActive();
      updateHeroChrome();
    }

    function updateHeroChrome() {
      const tabEl = document.getElementById('hero-active-tab');
      const dateEl = document.getElementById('hero-current-date');
      if (tabEl) tabEl.textContent = TAB_TITLES[currentTab] || 'DayView';
      if (dateEl) dateEl.textContent = currentDate ? fmtDateLong(currentDate) : '—';
    }

    function updateNavButtons() {
      const idx  = availableDays.indexOf(currentDate);
      const btnP = document.getElementById('btn-prev');
      const btnN = document.getElementById('btn-next');

      // days are newest-first or oldest-first? We sort ascending, so:
      // prev = earlier date (lower index if sorted asc) → idx - 1
      // next = later date → idx + 1
      if (btnP) btnP.disabled = (idx <= 0);
      if (btnN) btnN.disabled = (idx < 0 || idx >= availableDays.length - 1);
    }

    function navigateDay(offset) {
      const idx = availableDays.indexOf(currentDate);
      if (idx < 0) return;
      const newIdx = idx + offset;
      if (newIdx < 0 || newIdx >= availableDays.length) return;
      loadDay(availableDays[newIdx]);
    }

    // ── Render: Stats bar ──────────────────────────────────────────────

    function renderStats(stats, audioCount, roles, focusMinutes) {
      const bar = document.getElementById('stats-bar');
      if (!bar) return;

      const apps = stats?.unique_apps ?? 0;

      // Compute focus and meeting hours from roles data
      const focusHrs = focusMinutes > 0 ? `${(focusMinutes / 60).toFixed(1)}h focused` : '';
      const meetingRole = (roles || []).find(r => r.role === 'Meetings');
      const meetingHrs = meetingRole ? `${(meetingRole.minutes / 60).toFixed(1)}h meetings` : '';

      const parts = [];
      if (focusHrs) parts.push(focusHrs);
      if (meetingHrs) parts.push(meetingHrs);
      parts.push(`${apps} apps`);

      bar.innerHTML = parts.map(p => `<span>${p}</span>`).join('<span class="dot">\u00b7</span>');
    }

    // ── Render: Content (Summary + Insights + Activity Log) ───────────

    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      pollStart = null;
    }

    function setContentLoadingState() {
      const loadingHtml = `
        <div class="content-loading" aria-busy="true">
          <div class="spinner"></div> Analyzing your day…
        </div>`;
      const summaryEl = document.getElementById('summary-content');
      if (summaryEl) summaryEl.innerHTML = loadingHtml;
      // Show the insights/activities sections inside AI summary while loading
      document.getElementById('ai-section-insights')?.style.setProperty('display', 'block');
      document.getElementById('ai-section-activities')?.style.setProperty('display', 'block');
      const insightsEl = document.getElementById('insights-content');
      const activitiesEl = document.getElementById('activities-content');
      if (insightsEl) insightsEl.innerHTML = loadingHtml;
      if (activitiesEl) activitiesEl.innerHTML = loadingHtml;
      // Update toggle hint
      const hint = document.getElementById('ai-summary-toggle-hint');
      if (hint) hint.textContent = '— generating…';
    }

    function renderContent(content, generating) {
      const summaryEl = document.getElementById('summary-content');
      const insightsSection = document.getElementById('ai-section-insights');
      const activitiesSection = document.getElementById('ai-section-activities');
      const actionsSection = document.getElementById('ai-section-actions');
      const insightsEl = document.getElementById('insights-content');
      const activitiesEl = document.getElementById('activities-content');
      const hint = document.getElementById('ai-summary-toggle-hint');

      if (!content) {
        if (generating) {
          setContentLoadingState();
          startPolling();
        } else {
          // Content is null and not generating — show generate prompt
          stopPolling();
          if (summaryEl) summaryEl.innerHTML = `
            <p style="color:var(--text-muted);font-size:14px;margin-bottom:14px;">No summary yet for this day.</p>
            <button class="btn btn-primary" onclick="generateSummary(false)" aria-label="Generate daily summary">
              Generate Summary
            </button>`;
          if (insightsSection) insightsSection.style.display = 'none';
          if (activitiesSection) activitiesSection.style.display = 'none';
          if (actionsSection) actionsSection.style.display = 'none';
          if (hint) hint.textContent = '';
        }
        return;
      }

      stopPolling();

      // Day Summary card
      if (summaryEl) {
        const summaryText = content.summary ? esc(content.summary) : '<span style="color:var(--text-muted)">No summary available.</span>';
        summaryEl.innerHTML = `
          <p class="summary-text">${summaryText}</p>
          <div class="summary-footer">
            <button class="regenerate-link" onclick="generateSummary(true)" aria-label="Regenerate summary">
              &#8635; Regenerate
            </button>
          </div>`;
      }

      // Update toggle hint with brief summary excerpt
      if (hint && content.summary) {
        const excerpt = content.summary.length > 60 ? content.summary.slice(0, 60) + '…' : content.summary;
        hint.textContent = '— ' + excerpt;
      } else if (hint) {
        hint.textContent = '';
      }

      // Insights section (inside AI summary)
      const insights = Array.isArray(content.insights) ? content.insights : [];
      if (insights.length > 0) {
        if (insightsSection) insightsSection.style.display = 'block';
        if (insightsEl) {
          insightsEl.innerHTML = insights.map(insight => `
            <div class="insight-row">
              <span class="insight-icon" aria-hidden="true">&#9733;</span>
              <span class="insight-text">${esc(insight)}</span>
            </div>`).join('');
        }
      } else {
        if (insightsSection) insightsSection.style.display = 'none';
      }

      // Activity Log section (inside AI summary)
      const activities = Array.isArray(content.activities) ? content.activities : [];
      if (activities.length > 0) {
        if (activitiesSection) activitiesSection.style.display = 'block';
        if (activitiesEl) {
          const sorted = [...activities].sort((a, b) => (b.time || '').localeCompare(a.time || ''));
          const rows = sorted.map((act, i) => {
            const time = (act.time || '').replace(/\s*\(.*?\)\s*/g, '').trim();
            const desc = act.description || '';
            const needsExpand = desc.length > 200;
            const descId = `act-desc-${i}`;

            return `
              <div class="activity-row">
                <div class="activity-header">
                  ${time ? `<span class="activity-time">${esc(time)}</span>` : ''}
                  <span class="activity-title">${esc(act.title || '')}</span>
                </div>
                ${desc ? `<div class="activity-desc" id="${descId}">${esc(desc)}</div>` : ''}
                ${needsExpand ? `<button class="activity-expand" onclick="toggleActivityDesc('${descId}', this)">Show more</button>` : ''}
              </div>`;
          });
          activitiesEl.innerHTML = `<div class="activity-list">${rows.join('')}</div>`;
        }
      } else {
        if (activitiesSection) activitiesSection.style.display = 'none';
      }

      // Action Items section (inside AI summary)
      const nextSteps = Array.isArray(content.next_steps) ? content.next_steps : [];
      renderActionItems(nextSteps);
    }

    function startPolling() {
      if (pollTimer) return; // already polling
      pollStart = Date.now();
      const dateSnapshot = currentDate;

      pollTimer = setInterval(async () => {
        // Stop if user navigated away
        if (currentDate !== dateSnapshot) { stopPolling(); return; }

        // Timeout after 60 seconds
        if (Date.now() - pollStart > 60000) {
          stopPolling();
          const summaryEl = document.getElementById('summary-content');
          if (summaryEl) summaryEl.innerHTML = `
            <p style="color:var(--text-muted);font-size:14px;margin-bottom:14px;">
              Summary generation taking longer than expected.
            </p>
            <button class="btn btn-ghost" onclick="generateSummary(true)" aria-label="Retry summary generation">
              Retry
            </button>`;
          const hint = document.getElementById('ai-summary-toggle-hint');
          if (hint) hint.textContent = '— timed out';
          document.getElementById('ai-section-insights')?.style.setProperty('display', 'none');
          document.getElementById('ai-section-activities')?.style.setProperty('display', 'none');
          return;
        }

        try {
          const resp = await fetch(`/api/day/${dateSnapshot}`);
          if (!resp.ok) return;
          const data = await resp.json();
          if (data.content) {
            renderContent(data.content, false);
          }
        } catch { /* silent — keep polling */ }
      }, 3000);
    }

    async function generateSummary(force = false) {
      if (!currentDate) return;
      setContentLoadingState();

      try {
        const resp = await fetch(`/api/summarize/${currentDate}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force }),
        });

        if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
        const data = await resp.json();

        // Backend may return structured content directly, or signal generating=true
        if (data.content) {
          renderContent(data.content, false);
        } else {
          renderContent(null, true);
        }
      } catch (err) {
        setError('summary-content', `Failed to generate summary: ${err.message}`);
        document.getElementById('ai-section-insights')?.style.setProperty('display', 'none');
        document.getElementById('ai-section-activities')?.style.setProperty('display', 'none');
      }
    }

    // ── Render: Timeline ───────────────────────────────────────────────

    function renderTimeline(timeline) {
      const el = document.getElementById('timeline-content');
      if (!el) return;

      // Cache for role filtering re-renders
      lastTimeline = timeline;

      if (!Array.isArray(timeline) || timeline.length === 0) {
        setEmpty('timeline-content', 'No activity recorded for this day.');
        return;
      }

      // Apply role filter if active
      let filtered = timeline;
      if (roleFilter) {
        filtered = timeline.filter(session => (session.role || 'Other') === roleFilter);
        if (filtered.length === 0) {
          el.innerHTML = `<div class="empty-state">No ${esc(roleFilter)} sessions found.</div>`;
          return;
        }
      }

      const rows = filtered.map((session, i) => {
        const appName    = session.app || 'Unknown';
        const letter     = appName.charAt(0).toUpperCase();
        const color      = appColor(appName);
        const startFmt   = fmtTime(session.start);
        const endFmt     = fmtTime(session.end);
        const frames     = session.frame_count ?? session.frames ?? 0;
        const windows    = Array.isArray(session.windows) ? session.windows : [];
        const samples    = Array.isArray(session.samples)  ? session.samples  : [];
        const winCount   = windows.length;
        const sampId     = `samples-${i}`;

        const windowTags = windows.slice(0, 6).map(w =>
          `<span class="window-tag" title="${esc(w)}">${esc(w)}</span>`
        ).join('');

        const moreWin = windows.length > 6
          ? `<span class="window-tag" style="color:var(--text-muted)">+${windows.length - 6} more</span>`
          : '';

        const samplesHtml = samples.length > 0 ? `
          <button
            class="samples-toggle"
            onclick="toggleSamples('${sampId}', this)"
            aria-expanded="false"
            aria-controls="${sampId}"
          >Show samples &#9660;</button>
          <div class="samples-list" id="${sampId}" role="region">
            ${samples.map(s => `<div class="sample-item">${esc(s)}</div>`).join('')}
          </div>` : '';

        const sep = i < filtered.length - 1 ? '<div class="timeline-separator"></div>' : '';

        return `
          <div class="session-row">
            <div class="session-main">
              <span class="session-time">${esc(startFmt)} – ${esc(endFmt)}</span>
              <div class="session-app-row">
                <div class="app-icon" style="background:${color}" aria-hidden="true">${esc(letter)}</div>
                <span class="app-name">${esc(appName)}</span>
              </div>
              <div class="session-meta">
                ${winCount > 0 ? `${winCount} window${winCount !== 1 ? 's' : ''}  &bull;` : ''}
                ${frames} frame${frames !== 1 ? 's' : ''}
              </div>
            </div>
            ${windowTags || moreWin ? `<div class="session-windows">${windowTags}${moreWin}</div>` : ''}
            ${samplesHtml}
          </div>${sep}`;
      });

      el.innerHTML = `<div class="timeline-list">${rows.join('')}</div>`;
    }

    function renderRoleBar(roles) {
      const wrap = document.getElementById('usage-bar-wrap');
      if (!wrap || !Array.isArray(roles) || roles.length === 0) {
        if (wrap) wrap.innerHTML = '';
        return;
      }

      const segments = roles.map(r =>
        `<div class="role-bar-segment" title="${esc(r.role)}: ${r.pct}% (${r.minutes}m)" style="flex:${r.minutes};background:${safeColor(r.color)}"></div>`
      ).join('');

      const legend = roles.map(r =>
        `<div class="usage-bar-legend-item"><div class="usage-bar-dot" style="background:${safeColor(r.color)}"></div><span>${esc(r.role)} ${r.pct}%</span></div>`
      ).join('');

      wrap.innerHTML = `<div class="role-bar">${segments}</div><div class="usage-bar-legend">${legend}</div>`;
    }

    function renderRoleBreakdown(roles) {
      const section = document.getElementById('section-roles');
      const el = document.getElementById('roles-content');
      if (!section || !el) return;

      if (!Array.isArray(roles) || roles.length === 0) {
        section.classList.add('hidden');
        return;
      }

      section.classList.remove('hidden');

      const items = roles.map(r => {
        const hrs = Math.floor(r.minutes / 60);
        const mins = r.minutes % 60;
        const timeStr = hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`;
        const isActive = roleFilter === r.role;

        return `
          <div class="role-item${isActive ? ' active' : ''}" onclick="toggleRoleFilter('${esc(r.role)}')" title="Click to filter timeline">
            <div class="role-dot" style="background:${safeColor(r.color)}"></div>
            <div class="role-info">
              <div class="role-name">${esc(r.role)}</div>
              <div class="role-time">${timeStr}</div>
            </div>
            <span class="role-pct">${r.pct}%</span>
          </div>`;
      });

      el.innerHTML = `<div class="role-breakdown-list">${items.join('')}</div>`;
    }

    function toggleRoleFilter(role) {
      roleFilter = roleFilter === role ? null : role;
      renderRoleBreakdown(lastRolesData);
      renderTimeline(lastTimeline);
    }

    function renderActionItems(nextSteps) {
      const section = document.getElementById('ai-section-actions');
      const el = document.getElementById('actions-content');
      if (!el) return;

      if (!Array.isArray(nextSteps) || nextSteps.length === 0) {
        if (section) section.style.display = 'none';
        return;
      }

      if (section) section.style.display = 'block';

      const items = nextSteps.map(step => `
        <div class="action-item-row">
          <div class="action-checkbox"></div>
          <div class="action-content">
            <div class="action-text">${esc(step.item || '')}</div>
            ${step.context ? `<div class="action-context">${esc(step.context)}</div>` : ''}
          </div>
        </div>`);

      el.innerHTML = items.join('');
    }

    function toggleActivityDesc(id, btn) {
      const el = document.getElementById(id);
      if (!el) return;
      const expanded = el.classList.toggle('expanded');
      btn.textContent = expanded ? 'Show less' : 'Show more';
    }

    function toggleSamples(id, btn) {
      const el      = document.getElementById(id);
      const isOpen  = el.classList.toggle('open');
      btn.setAttribute('aria-expanded', String(isOpen));
      btn.textContent = isOpen ? 'Hide samples \u25B2' : 'Show samples \u25BC';
    }

    // ── Render: Meetings ───────────────────────────────────────────────

    function renderMeetings(meetings) {
      const el = document.getElementById('meetings-content');
      if (!el) return;

      if (!Array.isArray(meetings) || meetings.length === 0) {
        setEmpty('meetings-content', 'No meetings detected today.');
        return;
      }

      const appIcons = {
        'google meet': '&#127909;',
        'zoom':        '&#128249;',
        'teams':       '&#128101;',
        'webex':       '&#127911;',
        'facetime':    '&#128222;',
      };

      const cards = meetings.map((m, i) => {
        const appLower   = (m.app || '').toLowerCase();
        const icon       = Object.entries(appIcons).find(([k]) => appLower.includes(k))?.[1] ?? '&#128197;';
        const transcript = Array.isArray(m.transcript) ? m.transcript : [];
        const tId        = `transcript-${i}`;
        const tToggleId  = `ttoggle-${i}`;
        const duration   = m.duration_minutes ? `${m.duration_minutes} min` : '';

        const entries = transcript.map(entry => `
          <div class="transcript-entry">
            <span class="entry-time">${esc(entry.time || '')}</span>
            <span class="entry-speaker">${esc(entry.speaker || '')}</span>
            <span class="entry-text">${esc(entry.text || '')}</span>
          </div>`).join('');

        const transcriptSection = transcript.length > 0 ? `
          <button
            class="transcript-toggle"
            id="${tToggleId}"
            onclick="toggleTranscript('${tId}', '${tToggleId}')"
            aria-expanded="false"
            aria-controls="${tId}"
          >
            Transcript
            <span style="color:var(--text-muted);margin-left:4px">(${transcript.length} entr${transcript.length !== 1 ? 'ies' : 'y'})</span>
            <span class="toggle-arrow">&#9660;</span>
          </button>
          <div class="transcript-body" id="${tId}" role="region" aria-label="Meeting transcript">
            ${entries}
          </div>` : '';

        return `
          <div class="meeting-card">
            <div class="meeting-header">
              <span class="meeting-icon" aria-hidden="true">${icon}</span>
              <div class="meeting-info">
                <div class="meeting-app">${esc(m.app || 'Meeting')}</div>
                <div class="meeting-time">${esc(m.start || '')} – ${esc(m.end || '')}</div>
                ${m.title ? `<div class="meeting-title">"${esc(m.title)}"</div>` : ''}
              </div>
              ${duration ? `<span class="meeting-duration">${esc(duration)}</span>` : ''}
            </div>
            ${transcriptSection}
          </div>`;
      });

      el.innerHTML = `<div class="meetings-list">${cards.join('')}</div>`;
    }

    function toggleTranscript(bodyId, toggleId) {
      const body   = document.getElementById(bodyId);
      const toggle = document.getElementById(toggleId);
      if (!body || !toggle) return;

      const isOpen = body.classList.toggle('open');
      toggle.classList.toggle('open', isOpen);
      toggle.setAttribute('aria-expanded', String(isOpen));
    }

    // ── Render: Search results ─────────────────────────────────────────

    function renderSearchResults(results, keyword) {
      const el = document.getElementById('search-results');
      if (!el) return;

      if (!Array.isArray(results) || results.length === 0) {
        el.innerHTML = `<div class="empty-state">No results found.</div>`;
        return;
      }

      const items = results.map(r => {
        const type     = (r.type || 'ocr').toLowerCase();
        const typeLabel = type === 'audio' ? 'Audio' : 'OCR';
        const time     = r.timestamp ? fmtTime(r.timestamp) : '';
        const source   = r.app_name || r.device || '';
        const window_  = r.window_name || '';
        const text     = r.text || '';
        const snippet  = text.length > 280 ? text.slice(0, 280) + '…' : text;

        return `
          <div class="result-item">
            <div class="result-meta">
              <span class="result-type ${type}" aria-label="${typeLabel} result">${typeLabel}</span>
              ${time ? `<span class="result-time">${esc(time)}</span>` : ''}
              <span class="result-source">${esc(source)}${window_ ? ` — ${esc(window_)}` : ''}</span>
            </div>
            <div class="result-text">${highlightKeyword(snippet, keyword)}</div>
          </div>`;
      });

      el.innerHTML = items.join('');
    }

    // ── Search ─────────────────────────────────────────────────────────

    async function doSearch() {
      const input   = document.getElementById('search-input');
      const dayOnly = document.getElementById('search-current-day');
      const resEl   = document.getElementById('search-results');
      const btn     = document.getElementById('btn-search');

      const q = (input?.value || '').trim();
      if (!q) {
        if (resEl) resEl.innerHTML = '';
        return;
      }

      if (btn) { btn.disabled = true; btn.innerHTML = '<div class="spinner"></div>'; }
      if (resEl) resEl.innerHTML = `
        <div style="display:flex;align-items:center;gap:8px;color:var(--text-muted);font-size:13px;padding:8px 0">
          <div class="spinner"></div> Searching…
        </div>`;

      try {
        const dateParam = (dayOnly?.checked && currentDate) ? `&date=${currentDate}` : '';
        const resp = await fetch(`/api/search?q=${encodeURIComponent(q)}${dateParam}`);
        if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
        const data = await resp.json();
        renderSearchResults(data.results || [], q);
      } catch (err) {
        if (resEl) resEl.innerHTML = `<div class="error-state" role="alert">Search failed: ${esc(err.message)}</div>`;
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Search'; }
      }
    }

    // ── AI Summary toggle ──────────────────────────────────────────────

    function toggleAiSummary() {
      const toggle = document.getElementById('ai-summary-toggle');
      const body = document.getElementById('ai-summary-body');
      if (!toggle || !body) return;
      const isOpen = body.classList.toggle('open');
      toggle.classList.toggle('open', isOpen);
      toggle.setAttribute('aria-expanded', String(isOpen));
    }

    // ── Render: Your Day card ──────────────────────────────────────────

    function renderYourDay(roles, activityData) {
      const el = document.getElementById('your-day-content');
      if (!el) return;

      const hasRoles = Array.isArray(roles) && roles.length > 0;
      const projects = activityData?.projects || [];
      const hasProjects = projects.length > 0;

      if (!hasRoles && !hasProjects) {
        el.innerHTML = `<div class="empty-state">No screen time recorded for this day.</div>`;
        return;
      }

      let html = '';

      // Role bar + legend
      if (hasRoles) {
        const segments = roles.map(r =>
          `<div class="your-day-role-segment" title="${esc(r.role)}: ${r.pct}% (${r.minutes}m)" style="flex:${r.minutes};background:${safeColor(r.color)}"></div>`
        ).join('');

        const legend = roles.map(r => {
          const hrs = Math.floor(r.minutes / 60);
          const mins = r.minutes % 60;
          const timeStr = hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`;
          return `
            <div class="your-day-role-legend-item">
              <div class="your-day-role-dot" style="background:${safeColor(r.color)}"></div>
              <span>${esc(r.role)} <strong style="color:var(--text)">${timeStr}</strong></span>
            </div>`;
        }).join('');

        html += `
          <div class="role-bar-container">
            <div class="your-day-role-bar">${segments}</div>
            <div class="your-day-role-legend">${legend}</div>
          </div>`;
      }

      // Project time bars (top 8 only)
      if (hasProjects) {
        const shown = projects.slice(0, 8);
        const extra = projects.length - 8;
        const maxMin = Math.max(...shown.map(p => p.minutes || 0), 1);
        const rows = shown.map(p => {
          const pct = Math.max(((p.minutes || 0) / maxMin) * 100, 1);
          const label = p.minutes >= 60
            ? `${(p.minutes / 60).toFixed(1)}h`
            : `${Math.round(p.minutes)}m`;
          return `
            <div class="project-time-row">
              <span class="project-time-name" title="${esc(p.name)}">${esc(p.name)}</span>
              <div class="project-time-bar-wrap">
                <div class="project-time-bar" style="width:${pct}%"></div>
              </div>
              <span class="project-time-label">${label}</span>
            </div>`;
        }).join('');

        const moreHtml = extra > 0
          ? `<div style="font-size:12px;color:var(--text-muted);padding:4px 0 0 0;">+ ${extra} more projects</div>`
          : '';

        const sectionTitle = hasRoles
          ? `<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-muted);margin-bottom:8px;">Project Time</div>`
          : '';

        html += sectionTitle + rows + moreHtml;
      }

      el.innerHTML = html;
    }

    // ── Render: Shipped Today card ─────────────────────────────────────

    function renderShippedToday(shippedData) {
      const section = document.getElementById('section-shipped-today');
      const el = document.getElementById('shipped-today-content');
      if (!section || !el) return;

      // shippedData comes from /api/shipped?days=1 — now has projects[] shape
      const days = shippedData?.days || [];
      const tagColors = shippedData?.tag_colors || _shippedTagColors || {};
      const projects = days.length > 0 ? (days[0].projects || []) : [];

      // Flatten all items to check if any exist
      const allItems = projects.flatMap(pg => pg.items || []);
      if (allItems.length === 0) {
        section.classList.add('hidden');
        return;
      }

      section.classList.remove('hidden');

      // Render project-grouped view, cap at 15 items total
      let itemCount = 0;
      const MAX = 15;
      let html = '';
      for (const pg of projects) {
        if (itemCount >= MAX) break;
        const dotColor = tagColors[pg.project_tag] || '#64748B';
        html += `<div style="margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:6px;font-size:13px;font-weight:600;color:var(--text);margin-bottom:2px;">
            <span style="width:7px;height:7px;border-radius:50%;background:${dotColor};flex-shrink:0"></span>
            ${esc(pg.project_name)}
          </div>`;
        for (const item of pg.items) {
          if (itemCount >= MAX) break;
          const icon = item.type === 'done'
            ? `<span class="shipped-today-check" aria-hidden="true">&#10003;</span>`
            : item.type === 'blocker'
              ? `<span class="shipped-today-check" style="color:var(--error)" aria-hidden="true">&#9679;</span>`
              : `<span class="shipped-today-check" style="color:var(--accent)" aria-hidden="true">&#8594;</span>`;
          const commitBadge = item.commit_count ? `[${item.commit_count} commits] ` : '';
          html += `
            <div class="shipped-today-row" style="padding-left:13px">
              ${icon}
              <span class="shipped-today-text">${commitBadge}${esc(item.text)}</span>
            </div>`;
          itemCount++;
        }
        html += `</div>`;
      }
      const totalItems = allItems.length;
      if (totalItems > MAX) {
        html += `<div style="padding:6px 0 0 28px;font-size:12px;color:var(--text-muted);">+ ${totalItems - MAX} more</div>`;
      }
      el.innerHTML = html;
    }

    // ── Load day ───────────────────────────────────────────────────────

    async function loadDay(dateStr) {
      stopPolling();
      currentDate = dateStr;
      updateDateDisplay();

      // Reset sections to loading state
      setSkeletonLoading('your-day-content');
      setSkeletonLoading('timeline-content');
      setSkeletonLoading('meetings-content');

      // Hide shipped-today until data arrives
      document.getElementById('section-shipped-today')?.classList.add('hidden');

      // Collapse AI summary on day change and reset hint
      const aiBody = document.getElementById('ai-summary-body');
      const aiToggle = document.getElementById('ai-summary-toggle');
      const aiHint = document.getElementById('ai-summary-toggle-hint');
      if (aiBody) aiBody.classList.remove('open');
      if (aiToggle) { aiToggle.classList.remove('open'); aiToggle.setAttribute('aria-expanded', 'false'); }
      if (aiHint) aiHint.textContent = '';

      // Hide AI sub-sections
      document.getElementById('ai-section-insights')?.style.setProperty('display', 'none');
      document.getElementById('ai-section-activities')?.style.setProperty('display', 'none');
      document.getElementById('ai-section-actions')?.style.setProperty('display', 'none');

      // Reset role filter on day change
      roleFilter = null;
      lastRolesData = null;
      lastTimeline = null;

      // Clear search results on day change
      const resEl = document.getElementById('search-results');
      if (resEl) resEl.innerHTML = '';

      // Clear stats bar
      const statsBar = document.getElementById('stats-bar');
      if (statsBar) statsBar.innerHTML = '&nbsp;';

      try {
        // Fetch all four APIs in parallel; activity + shipped are best-effort
        const [dayResp, meetingsResp, activityResp, shippedResp] = await Promise.all([
          fetch(`/api/day/${dateStr}`),
          fetch(`/api/meetings/${dateStr}`),
          fetch(`/api/activity/${dateStr}`).catch(() => null),
          fetch(`/api/shipped?days=1`).catch(() => null),
        ]);

        if (!dayResp.ok)      throw new Error(`Day data: server returned ${dayResp.status}`);
        if (!meetingsResp.ok) throw new Error(`Meetings: server returned ${meetingsResp.status}`);

        const dayData      = await dayResp.json();
        const meetingsData = await meetingsResp.json();
        const activityData = (activityResp && activityResp.ok) ? await activityResp.json() : null;
        const shippedData = (shippedResp && shippedResp.ok) ? await shippedResp.json() : null;

        const roles = dayData.roles || [];
        lastRolesData = roles;

        // Render fast factual sections first
        renderYourDay(roles, activityData);
        renderShippedToday(shippedData);
        renderStats(dayData.stats || {}, dayData.audio_count, roles, dayData.focus_minutes ?? 0);
        renderRoleBar(roles);
        renderTimeline(dayData.timeline || []);
        renderMeetings(meetingsData.meetings || []);

        // Render AI content (may trigger polling if still generating)
        renderContent(dayData.content ?? null, dayData.generating ?? false);

      } catch (err) {
        setError('your-day-content', `Failed to load data: ${err.message}`);
        setError('timeline-content', `Failed to load data: ${err.message}`);
        setError('meetings-content', `Failed to load data: ${err.message}`);
      }
    }

    // ── Tab switching ──────────────────────────────────────────────────
    let dailyLoaded = false;

    function switchTab(tab) {
      currentTab = tab;
      document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
        b.setAttribute('aria-selected', String(b.dataset.tab === tab));
      });
      document.querySelectorAll('.tab-content').forEach(c => {
        c.classList.toggle('active', c.id === 'tab-' + tab);
      });
      // Show date nav only for daily view
      const dateNav = document.querySelector('.date-nav');
      if (dateNav) dateNav.style.display = tab === 'daily' ? '' : 'none';
      updateHeroChrome();

      if (tab === 'daily') {
        if (!dailyLoaded && currentDate) {
          dailyLoaded = true;
          loadDay(currentDate);
        }
      }

      if (tab === 'projects') {
        if (!projectsLoaded) loadProjects();
      }

      if (tab === 'overview') {
        if (!overviewLoaded) loadPortfolio();
      }

      if (tab === 'database') {
        if (!dbLoaded) loadDatabase();
      }
    }

    // ── Shipped tab (formerly Portfolio) ───────────────────────────────

    function fmtMinutes(mins) {
      if (!mins || mins <= 0) return '0m';
      const h = Math.floor(mins / 60);
      const m = Math.round(mins % 60);
      return h > 0 ? `${h}h ${m}m` : `${m}m`;
    }

    /** Format a date string like "2026-02-25" → "Wednesday, Feb 25" */
    function fmtDayHeading(dateStr) {
      if (!dateStr) return dateStr;
      try {
        const [y, mo, d] = dateStr.split('-').map(Number);
        const dt = new Date(y, mo - 1, d);
        return dt.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' });
      } catch { return dateStr; }
    }

    let _shippedTagColors = {};

    async function syncFromShipped() {
      const btn = document.getElementById('shipped-sync-btn');
      if (btn) { btn.disabled = true; btn.innerHTML = '<div class="spinner" style="width:14px;height:14px"></div> Syncing…'; }
      try {
        const resp = await fetch('/api/projects/sync', { method: 'POST' });
        if (!resp.ok) throw new Error(`Sync failed: ${resp.status}`);
        overviewLoaded = false;
        loadPortfolio();
      } catch (err) {
        alert('Sync error: ' + err.message);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Sync Now'; }
      }
    }

    // ── Inline editing functions ──────────────────────────────────────

    let _cachedProjectList = null;

    async function _fetchProjectList() {
      if (_cachedProjectList) return _cachedProjectList;
      const resp = await fetch('/api/projects?status=active');
      if (resp.ok) {
        const data = await resp.json();
        _cachedProjectList = data.projects || [];
      }
      return _cachedProjectList || [];
    }

    function startEditItem(el) {
      const bullet = el.closest('.shipped-bullet');
      if (!bullet || bullet.querySelector('.edit-input')) return;
      const textEl = bullet.querySelector('.bullet-text');
      const origText = textEl.textContent.trim();
      const input = document.createElement('input');
      input.className = 'edit-input';
      input.value = origText;
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); saveEditItem(input, origText); }
        if (e.key === 'Escape') { e.preventDefault(); cancelEdit(input, origText); }
      });
      input.addEventListener('blur', (e) => {
        if (e.relatedTarget && e.relatedTarget.classList.contains('item-action-btn')) {
          cancelEdit(input, origText);
          return;
        }
        saveEditItem(input, origText);
      });
      textEl.style.display = 'none';
      bullet.querySelector('.item-actions')?.style.setProperty('display', 'none');
      textEl.parentNode.insertBefore(input, textEl.nextSibling);
      input.focus();
      input.select();
    }

    function cancelEdit(input, origText) {
      const bullet = input.closest('.shipped-bullet');
      const textEl = bullet.querySelector('.bullet-text');
      textEl.style.display = '';
      input.remove();
    }

    async function saveEditItem(input, origText) {
      const bullet = input.closest('.shipped-bullet');
      if (!bullet) return;
      const newText = input.value.trim();
      if (!newText || newText === origText) { cancelEdit(input, origText); return; }
      const entryId = +bullet.dataset.entryId;
      const field = bullet.dataset.field;
      const itemIndex = +bullet.dataset.itemIndex;
      try {
        const resp = await fetch('/api/shipped/edit-item', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ entry_id: entryId, field, item_index: itemIndex, new_text: newText }),
        });
        if (!resp.ok) { const e = await resp.json(); throw new Error(e.error || 'Failed'); }
        overviewLoaded = false;
        loadPortfolio();
      } catch (err) {
        alert('Edit failed: ' + err.message);
        cancelEdit(input, origText);
      }
    }

    async function deleteItem(btn) {
      const bullet = btn.closest('.shipped-bullet');
      if (!bullet) return;
      const entryId = +bullet.dataset.entryId;
      const field = bullet.dataset.field;
      const itemIndex = +bullet.dataset.itemIndex;
      try {
        const resp = await fetch('/api/shipped/delete-item', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ entry_id: entryId, field, item_index: itemIndex }),
        });
        if (!resp.ok) { const e = await resp.json(); throw new Error(e.error || 'Failed'); }
        overviewLoaded = false;
        loadPortfolio();
      } catch (err) {
        alert('Delete failed: ' + err.message);
      }
    }

    async function moveItemUI(btn) {
      const bullet = btn.closest('.shipped-bullet');
      if (!bullet) return;
      // Close any existing dropdown
      document.querySelectorAll('.reassign-dropdown').forEach(d => d.remove());
      const projects = await _fetchProjectList();
      const currentPid = +bullet.dataset.pid;
      const dd = document.createElement('div');
      dd.className = 'reassign-dropdown';
      dd.innerHTML = projects
        .filter(p => p.id !== currentPid)
        .map(p => `<div class="reassign-option" data-target-pid="${p.id}">
          <span class="shipped-tag-dot" style="background:${_shippedTagColors[p.tag] || '#64748B'};width:7px;height:7px;border-radius:50%;flex-shrink:0"></span>
          ${esc(p.name)}
        </div>`).join('');
      bullet.appendChild(dd);
      dd.addEventListener('click', async e => {
        const opt = e.target.closest('.reassign-option');
        if (!opt) return;
        const targetPid = +opt.dataset.targetPid;
        const entryId = +bullet.dataset.entryId;
        const field = bullet.dataset.field;
        const itemIndex = +bullet.dataset.itemIndex;
        const dateStr = bullet.dataset.date;
        dd.remove();
        try {
          const resp = await fetch('/api/shipped/move-item', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entry_id: entryId, field, item_index: itemIndex, target_project_id: targetPid, date: dateStr }),
          });
          if (!resp.ok) { const e = await resp.json(); throw new Error(e.error || 'Failed'); }
          overviewLoaded = false;
          loadPortfolio();
        } catch (err) {
          alert('Move failed: ' + err.message);
        }
      });
      // Close dropdown on click outside
      setTimeout(() => {
        const closer = e => { if (!dd.contains(e.target)) { dd.remove(); document.removeEventListener('click', closer); } };
        document.addEventListener('click', closer);
      }, 0);
    }

    // ── Inline rename project from Shipped tab ──────────────────────
    function startRenameProject(el) {
      const nameDiv = el.closest('.shipped-project-name');
      const projectId = +nameDiv.dataset.renamePid;
      const origName = el.textContent.trim();
      const input = document.createElement('input');
      input.className = 'rename-input';
      input.value = origName;
      el.replaceWith(input);
      input.focus();
      input.select();
      const save = async () => {
        if (input._saved) return;
        input._saved = true;
        const newName = input.value.trim();
        if (!newName || newName === origName) {
          restoreSpan(origName);
          return;
        }
        try {
          const resp = await fetch(`/api/projects/${projectId}/rename`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName }),
          });
          if (!resp.ok) { const e = await resp.json(); throw new Error(e.error || 'Failed'); }
          _cachedProjectList = null;
          overviewLoaded = false;
          loadPortfolio();
        } catch (err) {
          alert('Rename failed: ' + err.message);
          restoreSpan(origName);
        }
      };
      function restoreSpan(text) {
        const span = document.createElement('span');
        span.className = 'project-name-text';
        span.textContent = text;
        span.setAttribute('ondblclick', 'startRenameProject(this)');
        input.replaceWith(span);
      }
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); save(); }
        if (e.key === 'Escape') { restoreSpan(origName); }
      });
      input.addEventListener('blur', save);
    }

    // ── Drag-and-drop between project groups ────────────────────────
    function onItemDragStart(e) {
      const bullet = e.target.closest('.shipped-bullet');
      if (!bullet) return;
      bullet.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('application/json', JSON.stringify({
        entryId: +bullet.dataset.entryId,
        field: bullet.dataset.field,
        itemIndex: +bullet.dataset.itemIndex,
        date: bullet.dataset.date,
        sourcePid: +bullet.dataset.pid,
      }));
    }

    function onItemDragEnd(e) {
      e.target.closest('.shipped-bullet')?.classList.remove('dragging');
      document.querySelectorAll('.shipped-project-group.drag-over').forEach(el => el.classList.remove('drag-over'));
    }

    function onProjectDragOver(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const group = e.target.closest('.shipped-project-group');
      if (group) group.classList.add('drag-over');
    }

    function onProjectDragLeave(e) {
      const group = e.target.closest('.shipped-project-group');
      if (group && !group.contains(e.relatedTarget)) group.classList.remove('drag-over');
    }

    async function onProjectDrop(e) {
      e.preventDefault();
      const group = e.target.closest('.shipped-project-group');
      if (!group) return;
      group.classList.remove('drag-over');
      let payload;
      try { payload = JSON.parse(e.dataTransfer.getData('application/json')); } catch { return; }
      const targetPid = +group.dataset.dropPid;
      const targetDate = group.dataset.dropDate;
      // Only allow drops within the same day
      if (targetDate !== payload.date) return;
      // No-op if dropping on the same project
      if (targetPid === payload.sourcePid) return;
      try {
        const resp = await fetch('/api/shipped/move-item', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ entry_id: payload.entryId, field: payload.field, item_index: payload.itemIndex, target_project_id: targetPid, date: payload.date }),
        });
        if (!resp.ok) { const err = await resp.json(); throw new Error(err.error || 'Failed'); }
        overviewLoaded = false;
        loadPortfolio();
      } catch (err) {
        alert('Move failed: ' + err.message);
      }
    }

    async function loadPortfolio() {
      const container = document.getElementById('tab-overview');
      if (!container) return;

      container.innerHTML = `
        <div class="shipped-wrap">
          <div style="display:flex;align-items:center;gap:10px;padding:60px 0;color:var(--text-muted);font-size:14px;justify-content:center;">
            <div class="spinner"></div> Loading…
          </div>
        </div>`;

      try {
        const resp = await fetch('/api/shipped?days=14');
        if (!resp.ok) throw new Error(`API returned ${resp.status}`);
        const data = await resp.json();
        overviewLoaded = true;
        _shippedTagColors = data.tag_colors || {};
        // Pre-cache project list for reassign dropdown
        _fetchProjectList().catch(() => {});
        renderShipped(data, container);
      } catch (err) {
        container.innerHTML = `
          <div class="shipped-wrap">
            <div class="error-state" role="alert">Failed to load shipped feed: ${esc(err.message)}</div>
          </div>`;
      }
    }

    function renderShipped(data, container) {
      const days  = data.days  || [];
      const tagColors = data.tag_colors || {};

      const todayStr = new Date().toISOString().slice(0, 10);
      const todayGroup = days.find(d => d.date === todayStr);
      const earlierDays = days.filter(d => d.date !== todayStr);

      // --- Render project groups for a day ---
      function renderProjectGroups(projects, dayDate) {
        return projects.map(pg => {
          const dotColor = tagColors[pg.project_tag] || '#64748B';
          const itemsHtml = pg.items.map(item => {
            const ind = item.type === 'done' ? '&#10003;'
                      : item.type === 'blocker' ? '&#9679;'
                      : '&#8594;';
            const commitBadge = item.commit_count
              ? `<span class="shipped-commit-count">[${item.commit_count} commits]</span> `
              : '';
            const isGit = item.source === 'git';
            const editable = !isGit && item.entry_id != null;
            const dataAttrs = editable
              ? `data-entry-id="${item.entry_id}" data-field="${esc(item.field)}" data-item-index="${item.item_index}" data-date="${esc(dayDate || '')}" data-pid="${pg.project_id}"`
              : '';
            const actionsHtml = editable
              ? `<span class="item-actions">
                   <button class="item-action-btn" onclick="moveItemUI(this)" title="Move to project">&#8594;</button>
                   <button class="item-action-btn delete" onclick="deleteItem(this)" title="Delete">&times;</button>
                 </span>`
              : '';
            const textClick = editable ? `onclick="startEditItem(this)"` : '';
            const dragAttrs = editable
              ? `draggable="true" ondragstart="onItemDragStart(event)" ondragend="onItemDragEnd(event)"`
              : '';
            return `
              <div class="shipped-bullet" ${dataAttrs} ${dragAttrs}>
                <span class="indicator ${esc(item.type)}">${ind}</span>
                <span class="bullet-text" ${textClick}>${commitBadge}${esc(item.text)}</span>
                ${actionsHtml}
              </div>`;
          }).join('');
          return `
            <div class="shipped-project-group" data-drop-pid="${pg.project_id}" data-drop-date="${esc(dayDate || '')}"
                 ondragover="onProjectDragOver(event)" ondragleave="onProjectDragLeave(event)" ondrop="onProjectDrop(event)">
              <div class="shipped-project-name" data-rename-pid="${pg.project_id}">
                <span class="shipped-tag-dot" style="background:${dotColor}"></span>
                <span class="project-name-text" ondblclick="startRenameProject(this)">${esc(pg.project_name)}</span>
              </div>
              ${itemsHtml}
            </div>`;
        }).join('');
      }

      // --- TODAY section ---
      let todayHtml = '';
      if (todayGroup && todayGroup.projects && todayGroup.projects.length > 0) {
        const projects = todayGroup.projects;
        const screenMin = todayGroup.screen_minutes || 0;
        const doneCount = projects.reduce((s, pg) => s + pg.items.filter(i => i.type === 'done').length, 0);

        todayHtml = `
          <div class="shipped-today-card">
            <div class="shipped-today-header">
              <div>
                <div class="shipped-today-title">Today</div>
                <div class="shipped-today-subtitle">${doneCount} shipped${screenMin > 0 ? ` &middot; ${fmtMinutes(screenMin)} screen time` : ''}</div>
              </div>
            </div>
            ${renderProjectGroups(projects, todayStr)}
          </div>`;
      } else {
        todayHtml = `
          <div class="shipped-today-card">
            <div class="shipped-today-header">
              <div>
                <div class="shipped-today-title">Today</div>
                <div class="shipped-today-subtitle" style="color:var(--text-muted)">No entries yet. Hit Sync to pull in today's work.</div>
              </div>
            </div>
          </div>`;
      }

      // --- EARLIER ---
      let earlierHtml = '';
      if (earlierDays.length > 0) {
        earlierHtml = `<div class="shipped-section-label">Earlier</div>` +
          earlierDays.map(dayGroup => {
            const projects = dayGroup.projects || [];
            const screenMin = dayGroup.screen_minutes || 0;
            const doneCount = projects.reduce((s, pg) => s + pg.items.filter(i => i.type === 'done').length, 0);

            const dayStats = [
              doneCount > 0 ? `<span style="color:var(--success)">${doneCount} shipped</span>` : '',
              screenMin > 0 ? `<span class="shipped-screen-badge">${fmtMinutes(screenMin)}</span>` : '',
            ].filter(Boolean).join(' &middot; ');

            return `
              <div class="shipped-day-section">
                <div class="shipped-day-heading">
                  ${esc(fmtDayHeading(dayGroup.date))}
                  <span>${dayStats}</span>
                </div>
                ${renderProjectGroups(projects, dayGroup.date)}
              </div>`;
          }).join('');
      }

      container.innerHTML = `
        <div class="shipped-wrap">
          <div class="shipped-header-bar">
            <div></div>
            <button class="btn btn-ghost" id="shipped-sync-btn" onclick="syncFromShipped()">Sync Now</button>
          </div>
          ${todayHtml}
          ${earlierHtml}
        </div>`;
    }

    // ── Activity tab ────────────────────────────────────────────────────

    let activityDays = 7;
    let dbLoaded = false;  // keep variable name for switchTab compatibility

    async function loadDatabase() {  // keep function name for switchTab compatibility
      const container = document.getElementById('tab-database');
      if (!container) return;

      container.innerHTML = `<div class="activity-wrap"><div style="display:flex;align-items:center;gap:10px;padding:60px 0;color:var(--text-muted);font-size:14px;justify-content:center;"><div class="spinner"></div> Loading activity...</div></div>`;

      try {
        const resp = await fetch(`/api/activity_summary?days=${activityDays}`);
        if (!resp.ok) throw new Error(`API returned ${resp.status}`);
        const data = await resp.json();
        dbLoaded = true;
        renderActivity(data, container);
      } catch (err) {
        container.innerHTML = `<div class="activity-wrap"><div class="error-state">Failed to load: ${esc(err.message)}</div></div>`;
      }
    }

    function renderActivity(data, container) {
      const roles = data.roles || [];
      const topApps = data.top_apps || [];
      const topUrls = data.top_urls || [];
      const totalMin = data.total_screen_minutes || 0;

      // Role bar
      const totalRoleMin = roles.reduce((s, r) => s + r.minutes, 0) || 1;
      const roleBarHtml = roles.length > 0 ? `
        <div class="activity-role-bar">
          ${roles.map(r => {
            const pct = Math.max((r.minutes / totalRoleMin) * 100, 3);
            return `<div class="activity-role-segment" style="flex:${pct};background:${r.color}" title="${r.role}: ${fmtMinutes(r.minutes)}">${pct > 12 ? r.role : ''}</div>`;
          }).join('')}
        </div>
        <div class="activity-role-legend">
          ${roles.map(r => `
            <div class="activity-role-legend-item">
              <div class="activity-role-swatch" style="background:${r.color}"></div>
              ${r.role} <strong>${fmtMinutes(r.minutes)}</strong>
            </div>
          `).join('')}
        </div>` : '';

      // Top apps
      const maxAppMin = topApps.length > 0 ? topApps[0].minutes : 1;
      const appsHtml = topApps.length > 0 ? `
        <div class="activity-section-title">Top Apps</div>
        ${topApps.map(a => `
          <div class="activity-app-row">
            <span class="activity-app-name">${esc(a.app)}</span>
            <div class="activity-app-bar-wrap">
              <div class="activity-app-bar" style="width:${Math.max((a.minutes / maxAppMin) * 100, 1)}%"></div>
            </div>
            <span class="activity-app-time">${fmtMinutes(a.minutes)}</span>
          </div>
        `).join('')}
      ` : '';

      // Top URLs
      const urlsHtml = topUrls.length > 0 ? `
        <div class="activity-section-title" style="margin-top:24px">Top Sites</div>
        <div class="activity-url-list">
          ${topUrls.map(u => `
            <div class="activity-url-row">
              <span class="activity-url-domain">${esc(u.domain)}</span>
              <span class="activity-url-time">${fmtMinutes(u.minutes)}</span>
            </div>
          `).join('')}
        </div>` : '';

      // Search
      const searchHtml = `
        <div class="activity-search-wrap">
          <div class="activity-section-title" style="margin-top:24px">Search Screen Content</div>
          <input type="search" class="activity-search" id="activity-search-input"
            placeholder="Search what was on your screen..." autocomplete="off" />
          <div class="activity-search-results" id="activity-search-results"></div>
        </div>`;

      container.innerHTML = `
        <div class="activity-wrap">
          <div class="activity-header-bar">
            <div>
              <div class="activity-total">${fmtMinutes(totalMin)}</div>
              <div class="activity-total-label">Screen Time (${activityDays} days)</div>
            </div>
            <div class="activity-range-btns">
              ${[7, 14, 30].map(d => `
                <button class="activity-range-btn ${activityDays === d ? 'active' : ''}"
                  onclick="activityDays=${d}; loadDatabase()">${d}d</button>
              `).join('')}
            </div>
          </div>
          ${roleBarHtml}
          ${appsHtml}
          ${urlsHtml}
          ${searchHtml}
        </div>`;

      // Wire search
      let searchTimeout;
      const searchInput = document.getElementById('activity-search-input');
      if (searchInput) {
        searchInput.addEventListener('input', e => {
          clearTimeout(searchTimeout);
          searchTimeout = setTimeout(async () => {
            const q = e.target.value.trim();
            const resultsDiv = document.getElementById('activity-search-results');
            if (!q || q.length < 3) { resultsDiv.innerHTML = ''; return; }
            try {
              const resp = await fetch(`/api/activity_summary?q=${encodeURIComponent(q)}`);
              const data = await resp.json();
              const results = data.search_results || [];
              if (results.length === 0) {
                resultsDiv.innerHTML = '<div style="padding:12px 0;color:var(--text-muted);font-size:13px;">No results found.</div>';
                return;
              }
              resultsDiv.innerHTML = results.slice(0, 20).map(r => `
                <div class="activity-search-result">
                  <div class="activity-search-result-meta">${esc(r.app_name || r.device || '')} &middot; ${esc((r.timestamp || '').slice(0, 19))}</div>
                  <div class="activity-search-result-text">${esc((r.text || '').slice(0, 300))}</div>
                </div>
              `).join('');
            } catch { resultsDiv.innerHTML = ''; }
          }, 500);
        });
      }
    }

    // ── Projects tab ───────────────────────────────────────────────────

    function sourceIcon(source) {
      const s = (source || '').toLowerCase();
      if (s.includes('google') || s.includes('doc') || s.includes('gdoc')) {
        return `<span class="source-icon gdoc" title="Google Doc">G</span>`;
      }
      if (s.includes('slack')) {
        return `<span class="source-icon slack" title="Slack">S</span>`;
      }
      if (s.includes('linear')) {
        return `<span class="source-icon linear" title="Linear">L</span>`;
      }
      return `<span class="source-icon other" title="${esc(source || 'unknown')}">#</span>`;
    }

    async function loadProjects() {
      const container = document.getElementById('tab-projects');
      if (!container) return;
      container.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:center;gap:10px;padding:60px 0;color:var(--text-muted);font-size:14px;">
          <div class="spinner"></div> Loading projects…
        </div>`;

      try {
        const resp = await fetch('/api/projects');
        if (!resp.ok) throw new Error(`API returned ${resp.status}`);
        const data = await resp.json();
        projectsData = data.projects || [];
        projectsLoaded = true;
        renderProjectsList();
      } catch (err) {
        container.innerHTML = `
          <div style="max-width:600px;margin:60px auto;padding:0 16px;">
            <div class="error-state" role="alert">Failed to load projects: ${esc(err.message)}</div>
          </div>`;
      }
    }

    function renderProjectsList() {
      const container = document.getElementById('tab-projects');
      if (!container) return;
      currentProjectDetail = null;

      // Filter by status/blockers and search query
      const q = projectsSearch.trim().toLowerCase();
      let visible;
      if (currentFilter === '__blockers') {
        visible = projectsData.filter(p =>
          p.latest_entry && p.latest_entry.blockers && p.latest_entry.blockers.length > 0
        );
      } else if (currentFilter === '__stale') {
        visible = projectsData.filter(p =>
          p.status === 'active' && (p.recent_minutes || 0) === 0 && (p.activity_days || 0) === 0
        );
      } else if (currentFilter) {
        visible = projectsData.filter(p => p.status === currentFilter);
      } else {
        visible = projectsData.slice();
      }

      if (q) {
        visible = visible.filter(p =>
          p.name.toLowerCase().includes(q) ||
          (p.description || '').toLowerCase().includes(q) ||
          (p.last_achievement || '').toLowerCase().includes(q)
        );
      }

      // Sort
      const now = Date.now();
      visible.sort((a, b) => {
        if (projectsSort === 'name') {
          return a.name.localeCompare(b.name);
        }
        if (projectsSort === 'activity') {
          return (b.recent_minutes || 0) - (a.recent_minutes || 0);
        }
        if (projectsSort === 'entries') {
          return (b.entry_count || 0) - (a.entry_count || 0);
        }
        // default: updated (newest first)
        const ta = a.updated_at ? new Date(a.updated_at).getTime() : 0;
        const tb = b.updated_at ? new Date(b.updated_at).getTime() : 0;
        return tb - ta;
      });

      // Build status counts from full dataset (not filtered)
      const statusCounts = {};
      projectsData.forEach(p => { statusCounts[p.status] = (statusCounts[p.status] || 0) + 1; });

      const blockerCount = projectsData.filter(p =>
        p.latest_entry && p.latest_entry.blockers && p.latest_entry.blockers.length > 0
      ).length;

      const staleCount = projectsData.filter(p =>
        p.status === 'active' && (p.recent_minutes || 0) === 0 && (p.activity_days || 0) === 0
      ).length;

      // Toolbar (unchanged)
      const toolbarHtml = `
        <div class="projects-toolbar">
          <div class="projects-toolbar-row1">
            <h2>${visible.length} Project${visible.length !== 1 ? 's' : ''}</h2>
            <div class="projects-search-wrap">
              <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <circle cx="8.5" cy="8.5" r="5.5"/><path d="M15 15l-3.5-3.5"/>
              </svg>
              <input
                type="search"
                class="projects-search"
                id="projects-search-input"
                placeholder="Filter by name…"
                value="${esc(projectsSearch)}"
                autocomplete="off"
                aria-label="Search projects"
              />
            </div>
            <select class="projects-sort" id="projects-sort-select" aria-label="Sort projects">
              <option value="updated"${projectsSort === 'updated' ? ' selected' : ''}>Last updated</option>
              <option value="activity"${projectsSort === 'activity' ? ' selected' : ''}>Most active</option>
              <option value="name"${projectsSort === 'name' ? ' selected' : ''}>Name</option>
              <option value="entries"${projectsSort === 'entries' ? ' selected' : ''}>Most entries</option>
            </select>
            <button class="btn-sync-projects" onclick="createProjectInline()" aria-label="Create project" style="padding:4px 10px;font-size:16px;line-height:1;">+</button>
            <button class="btn-sync-projects" onclick="syncProjects()" id="btn-sync" aria-label="Sync projects">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
                <path d="M1 4v6h6M23 20v-6h-6"/>
                <path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10M23 14l-4.64 4.36A9 9 0 0 1 3.51 15"/>
              </svg>
              Sync
            </button>
            <span class="sync-status" id="sync-status"></span>
          </div>
          <div class="projects-toolbar-row2">
            <button class="filter-chip ${!currentFilter ? 'active' : ''}" onclick="setProjectFilter(null)">
              All <span style="opacity:0.7">(${projectsData.length})</span>
            </button>
            ${Object.entries(statusCounts).map(([s, c]) =>
              `<button class="filter-chip ${currentFilter === s ? 'active' : ''}" onclick="setProjectFilter('${esc(s)}')">${esc(s)} <span style="opacity:0.7">(${c})</span></button>`
            ).join('')}
            ${blockerCount > 0
              ? `<button class="filter-chip ${currentFilter === '__blockers' ? 'active' : ''}"
                  onclick="setProjectFilter('__blockers')"
                  style="border-color:rgba(248,113,113,0.4);color:var(--error);">
                  Blockers <span style="opacity:0.7">(${blockerCount})</span>
                </button>`
              : ''
            }
            ${staleCount > 0
              ? `<button class="filter-chip ${currentFilter === '__stale' ? 'active' : ''}"
                  onclick="setProjectFilter('__stale')"
                  style="border-color:rgba(251,191,36,0.4);color:var(--warning);">
                  Stale <span style="opacity:0.7">(${staleCount})</span>
                </button>`
              : ''
            }
          </div>
          <div class="create-project-inline hidden" id="create-project-form">
            <input type="text" id="new-project-name" placeholder="Project name" onkeydown="if(event.key==='Enter'){event.preventDefault();submitCreateProject();}"/>
            <select id="new-project-tag">
              <option value="">No tag</option>
              ${Object.keys(_shippedTagColors || {}).map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('')}
            </select>
            <button class="btn btn-ghost" onclick="submitCreateProject()" style="font-size:12px;padding:4px 10px;">Create</button>
          </div>
        </div>`;

      if (visible.length === 0) {
        container.innerHTML = `
          ${toolbarHtml}
          <div class="projects-tier-list">
            <div style="text-align:center;padding:40px;color:var(--text-muted);font-size:14px;">
              No projects match${q ? ` "${esc(q)}"` : ' this filter'}.
            </div>
          </div>`;
        wireProjectsToolbar(container);
        return;
      }

      // ── Tier grouping ───────────────────────────────────────────
      // Active:           status === 'active' AND recent_minutes > 0
      // Needs Attention:  status === 'active' AND recent_minutes === 0
      // Archived:         status === 'paused' OR 'completed'
      const activeProjects = visible.filter(p =>
        p.status === 'active' && (p.recent_minutes || 0) > 0
      );
      const needsAttention = visible.filter(p =>
        p.status === 'active' && (p.recent_minutes || 0) === 0
      );
      const archivedProjects = visible.filter(p =>
        p.status === 'paused' || p.status === 'completed'
      );

      function buildTierRow(p) {
        const recentMin = p.recent_minutes || 0;
        const isActive = p.status === 'active' && recentMin > 0;
        const isStale  = p.status === 'active' && recentMin === 0;

        // Last achievement: prefer p.last_achievement, fall back to latest entry
        let achievement = p.last_achievement || '';
        if (!achievement && p.latest_entry) {
          const entry = p.latest_entry;
          if (entry.achievements && entry.achievements.length > 0) {
            achievement = entry.achievements[0];
          } else if (entry.in_progress && entry.in_progress.length > 0) {
            achievement = entry.in_progress[0];
          }
        }
        const achievementTrunc = achievement.length > 65
          ? achievement.slice(0, 65) + '…'
          : achievement;

        // Badge
        let badgeHtml = '';
        if (isActive) {
          const label = recentMin >= 60 ? `${(recentMin/60).toFixed(1)}h` : `${Math.round(recentMin)}m`;
          badgeHtml = `<span class="project-tier-row-badge active-badge">${label}</span>`;
        } else if (isStale) {
          // Compute last-seen label
          let lastSeenStr = '';
          if (p.last_activity_date) {
            const ms = now - new Date(p.last_activity_date + 'T12:00:00').getTime();
            const days = Math.floor(ms / 86400000);
            if (days === 0) lastSeenStr = 'today';
            else if (days === 1) lastSeenStr = 'yesterday';
            else lastSeenStr = `${days}d ago`;
          }
          badgeHtml = `<span class="project-tier-row-badge stale-badge-inline">&#9888; ${lastSeenStr || 'stale'}</span>`;
        } else {
          const statusLabel = p.status === 'completed' ? 'done' : p.status;
          badgeHtml = `<span class="project-tier-row-badge archived-badge">${esc(statusLabel)}</span>`;
        }

        const hasBlockers = p.latest_entry?.blockers?.length > 0;

        const blockerStyle = hasBlockers ? ' style="border-left:3px solid var(--error)"' : '';
        return `
          <div class="project-tier-row"${blockerStyle}
            onclick="showProjectDetail(${p.id})"
            role="button"
            tabindex="0"
            aria-label="${esc(p.name)}${hasBlockers ? ', has blockers' : ''}">
            <span class="project-tier-row-name" title="${esc(p.name)}">${esc(p.name)}</span>
            <span class="project-tier-row-achievement">${esc(achievementTrunc)}</span>
            ${badgeHtml}
          </div>`;
      }

      function buildTierSection(title, projects, collapsedByDefault) {
        if (projects.length === 0) return '';
        const sectionId = `tier-rows-${title.replace(/\s+/g, '-').toLowerCase()}`;
        const isCollapsed = collapsedByDefault;
        return `
          <div class="project-tier-section">
            <div class="project-tier-header">
              <span>${esc(title)}</span>
              <span class="tier-count">(${projects.length})</span>
              ${collapsedByDefault ? `
                <button class="tier-toggle" onclick="toggleTierSection('${sectionId}', this)" aria-expanded="${!isCollapsed}" aria-controls="${sectionId}">
                  ${isCollapsed ? 'Show &#9660;' : 'Hide &#9650;'}
                </button>` : ''}
            </div>
            <div class="project-tier-rows${isCollapsed ? ' collapsed' : ''}" id="${sectionId}">
              ${projects.map(p => buildTierRow(p)).join('')}
            </div>
          </div>`;
      }

      const tierHtml = [
        buildTierSection('Active', activeProjects, false),
        buildTierSection('Needs Attention', needsAttention, false),
        buildTierSection('Paused / Completed', archivedProjects, true),
      ].join('');

      container.innerHTML = `
        ${toolbarHtml}
        <div class="projects-tier-list">
          ${tierHtml || `<div style="text-align:center;padding:40px;color:var(--text-muted);font-size:14px;">No projects to show.</div>`}
        </div>`;

      wireProjectsToolbar(container);
    }

    function toggleTierSection(sectionId, btn) {
      const rows = document.getElementById(sectionId);
      if (!rows) return;
      const isCollapsed = rows.classList.toggle('collapsed');
      btn.setAttribute('aria-expanded', String(!isCollapsed));
      btn.innerHTML = isCollapsed ? 'Show &#9660;' : 'Hide &#9650;';
    }

    function wireProjectsToolbar(container) {
      // Wire up search input live filtering
      const searchInput = document.getElementById('projects-search-input');
      if (searchInput) {
        searchInput.addEventListener('input', e => {
          projectsSearch = e.target.value;
          renderProjectsList();
        });
        if (projectsSearch) { searchInput.focus(); searchInput.setSelectionRange(9999, 9999); }
      }

      // Wire up sort select
      const sortSelect = document.getElementById('projects-sort-select');
      if (sortSelect) {
        sortSelect.addEventListener('change', e => {
          projectsSort = e.target.value;
          renderProjectsList();
        });
      }

      // Keyboard activation for tier rows
      container.querySelectorAll('.project-tier-row').forEach(row => {
        row.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            row.click();
          }
        });
      });
    }

    function setProjectFilter(filter) {
      currentFilter = filter;
      renderProjectsList();
    }

    async function showProjectDetail(projectId) {
      const container = document.getElementById('tab-projects');
      if (!container) return;
      container.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:center;gap:10px;padding:60px 0;color:var(--text-muted);font-size:14px;">
          <div class="spinner"></div> Loading…
        </div>`;

      try {
        const [resp, actResp] = await Promise.all([
          fetch(`/api/projects/${projectId}`),
          fetch(`/api/projects/${projectId}/activity?days=14`),
        ]);
        if (!resp.ok) throw new Error(`API returned ${resp.status}`);
        const data = await resp.json();
        const actData = actResp.ok ? await actResp.json() : { activity: [] };
        currentProjectDetail = projectId;

        const p = data.project;
        const timeline = data.timeline || [];
        const statusClass = p.status.replace(/\s+/g, '_');

        // Compute aggregate stats
        let totalDone = 0, totalWip = 0, totalBlockers = 0;
        timeline.forEach(e => {
          totalDone     += (e.achievements || []).length;
          totalWip      += (e.in_progress  || []).length;
          totalBlockers += (e.blockers     || []).length;
        });

        // Build timeline HTML
        let timelineBody = '';
        if (timeline.length === 0) {
          timelineBody = `<div style="color:var(--text-muted);font-size:13px;padding:20px 0;text-align:center;">No progress entries yet.</div>`;
        } else {
          // Group by date (could be multiple entries per date from different sources)
          const byDate = {};
          timeline.forEach(e => {
            if (!byDate[e.date]) byDate[e.date] = [];
            byDate[e.date].push(e);
          });

          const dates = Object.keys(byDate).sort().reverse();
          timelineBody = dates.map((d, idx) => {
            const dayEntries = byDate[d];
            const dt = new Date(d + 'T12:00:00');
            const dayLabel = dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
            const yearLabel = dt.getFullYear() !== new Date().getFullYear() ? String(dt.getFullYear()) : '';

            const dayHasBlockers = dayEntries.some(e => e.blockers && e.blockers.length > 0);

            const sections = [];

            dayEntries.forEach(e => {
              if (e.achievements && e.achievements.length) {
                sections.push(`
                  <div class="timeline-section">
                    <div class="timeline-section-label done-label">Done</div>
                    ${e.achievements.map(a => `
                      <div class="timeline-item">
                        <div class="timeline-item-dot done"></div>
                        <span>${esc(a)}</span>
                      </div>`).join('')}
                  </div>`);
              }
              if (e.in_progress && e.in_progress.length) {
                sections.push(`
                  <div class="timeline-section">
                    <div class="timeline-section-label wip-label">In Progress</div>
                    ${e.in_progress.map(a => `
                      <div class="timeline-item">
                        <div class="timeline-item-dot wip"></div>
                        <span>${esc(a)}</span>
                      </div>`).join('')}
                  </div>`);
              }
              if (e.blockers && e.blockers.length) {
                sections.push(`
                  <div class="timeline-section">
                    <div class="timeline-section-label blockers-label">Blockers</div>
                    ${e.blockers.map(b => `
                      <div class="timeline-blocker-item">
                        <div class="timeline-item-dot blocker"></div>
                        <span>${esc(b)}</span>
                      </div>`).join('')}
                  </div>`);
              }
            });

            return `
              <div class="project-timeline-entry">
                <div class="project-timeline-date-col">
                  <span class="timeline-date-label">${dayLabel}</span>
                  ${yearLabel ? `<span class="timeline-date-sub">${yearLabel}</span>` : ''}
                </div>
                <div class="project-timeline-dot${dayHasBlockers ? ' has-blockers' : ''}"></div>
                <div class="project-timeline-body">
                  ${sections.join('')}
                </div>
              </div>`;
          }).join('');
        }

        // Build activity chart from API data
        const actRows = actData.activity || [];
        const totalActMin = actRows.reduce((s, r) => s + (r.minutes || 0), 0);
        const activeDayCount = actRows.filter(r => r.frame_count > 0).length;
        const maxMin = Math.max(...actRows.map(r => r.minutes || 0), 1);

        let activityChartHtml = '';
        if (actRows.length > 0) {
          const barsHtml = actRows.map(r => {
            const pct = Math.max(((r.minutes || 0) / maxMin) * 100, 0);
            const dt = new Date(r.date + 'T12:00:00');
            const dayLbl = dt.toLocaleDateString('en-US', { weekday: 'narrow' });
            const dateLbl = `${dt.getMonth()+1}/${dt.getDate()}`;
            const minLbl = r.minutes >= 60 ? `${(r.minutes/60).toFixed(1)}h` : `${Math.round(r.minutes)}m`;
            const cls = r.frame_count > 0 ? 'activity-bar' : 'activity-bar zero';
            return `<div class="activity-bar-col" title="${r.date}: ${minLbl}">
              <div class="${cls}" style="height:${Math.max(pct, 2)}%"></div>
              <div class="activity-bar-label">${dayLbl}<br>${dateLbl}</div>
            </div>`;
          }).join('');

          const totalLabel = totalActMin >= 60
            ? `${(totalActMin/60).toFixed(1)}h`
            : `${Math.round(totalActMin)}m`;

          activityChartHtml = `
            <div class="activity-chart-card">
              <p class="activity-chart-heading">Screen Time (14 days)</p>
              <div class="activity-bars">${barsHtml}</div>
              <div class="activity-summary-row">
                <span><strong>${totalLabel}</strong> total</span>
                <span><strong>${activeDayCount}</strong> active days</span>
                <span><strong>${activeDayCount > 0 ? Math.round(totalActMin / activeDayCount) : 0}m</strong> avg/day</span>
              </div>
            </div>`;
        }

        container.innerHTML = `
          <div class="project-detail-wrap">
            <button class="project-detail-back" onclick="renderProjectsList()">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
                <path d="M19 12H5M12 5l-7 7 7 7"/>
              </svg>
              Back to projects
            </button>

            <div class="project-detail-header">
              <div class="project-detail-title-row">
                <span class="project-detail-name" onclick="renameProjectInline(this, ${p.id})" style="cursor:pointer" title="Click to rename">${esc(p.name)}</span>
                <span class="project-status-badge ${statusClass}">${esc(p.status)}</span>
              </div>
              ${p.description ? `<div class="project-detail-desc">${esc(p.description)}</div>` : ''}
              <div class="project-detail-meta">
                <span>${sourceIcon(p.source)} ${esc(p.source || 'unknown')}</span>
                ${p.created_at ? `<span>Created ${p.created_at.slice(0, 10)}</span>` : ''}
                ${p.updated_at ? `<span>Last updated ${p.updated_at.slice(0, 10)}</span>` : ''}
                <span style="margin-left:auto"><a href="#" onclick="event.preventDefault();deleteProjectConfirm(${p.id},'${esc(p.name).replace(/'/g, "\\'")}')" style="color:var(--error);font-size:12px;opacity:0.7">Delete project</a></span>
              </div>
              <div class="project-detail-stats">
                <div class="detail-stat">
                  <span class="detail-stat-value">${p.entry_count || 0}</span>
                  <span class="detail-stat-label">Entries</span>
                </div>
                <div class="detail-stat">
                  <span class="detail-stat-value" style="color:var(--success)">${totalDone}</span>
                  <span class="detail-stat-label">Done</span>
                </div>
                <div class="detail-stat">
                  <span class="detail-stat-value" style="color:var(--accent)">${totalWip}</span>
                  <span class="detail-stat-label">In Progress</span>
                </div>
                ${totalBlockers > 0 ? `
                <div class="detail-stat">
                  <span class="detail-stat-value" style="color:var(--error)">${totalBlockers}</span>
                  <span class="detail-stat-label">Blockers</span>
                </div>` : ''}
              </div>
            </div>

            ${activityChartHtml}

            <div class="project-timeline-card">
              <p class="project-timeline-card-heading">Progress Timeline</p>
              <div class="project-timeline">
                ${timelineBody}
              </div>
            </div>
          </div>`;
      } catch (err) {
        container.innerHTML = `
          <div style="max-width:600px;margin:60px auto;padding:0 16px;">
            <div class="error-state" role="alert">Failed to load project: ${esc(err.message)}</div>
          </div>`;
      }
    }

    async function syncProjects() {
      const btn    = document.getElementById('btn-sync');
      const status = document.getElementById('sync-status');

      if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<div class="spinner" style="border-color:rgba(74,158,255,0.3);border-top-color:var(--accent);"></div> Syncing…`;
      }
      if (status) status.textContent = '';

      try {
        const resp = await fetch('/api/projects/sync', { method: 'POST' });
        if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
        const data = await resp.json();

        // Update sync status with source icons + timestamp
        const now = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
        if (status) {
          const sourceIcons = [
            sourceIcon('gdoc'),
            sourceIcon('slack'),
          ].join('');
          status.innerHTML = `${sourceIcons} <span>${data.projects_synced} projects · ${data.entries_added} entries · ${now}</span>`;
        }

        projectsLoaded = false;
        await loadProjects();
      } catch (err) {
        if (status) status.textContent = `Sync failed: ${err.message}`;
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M1 4v6h6M23 20v-6h-6"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10M23 14l-4.64 4.36A9 9 0 0 1 3.51 15"/></svg> Sync`;
        }
      } finally {
        if (btn && !btn.disabled) return;
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M1 4v6h6M23 20v-6h-6"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10M23 14l-4.64 4.36A9 9 0 0 1 3.51 15"/></svg> Sync`;
        }
      }
    }

    // ── Project management (rename, delete, create) ─────────────────

    function renameProjectInline(el, projectId) {
      if (el.querySelector('input')) return;
      const origName = el.textContent.trim();
      const input = document.createElement('input');
      input.value = origName;
      input.style.cssText = 'font-size:inherit;font-weight:inherit;background:var(--bg);color:var(--text);border:1px solid var(--accent);border-radius:4px;padding:2px 6px;width:260px;';
      el.textContent = '';
      el.appendChild(input);
      input.focus();
      input.select();
      const save = async () => {
        const newName = input.value.trim();
        if (!newName || newName === origName) { el.textContent = origName; return; }
        try {
          const resp = await fetch(`/api/projects/${projectId}/rename`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName }),
          });
          if (!resp.ok) { const e = await resp.json(); throw new Error(e.error); }
          _cachedProjectList = null;
          projectsLoaded = false;
          overviewLoaded = false;
          showProjectDetail(projectId);
        } catch (err) { alert('Rename failed: ' + err.message); el.textContent = origName; }
      };
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); save(); } if (e.key === 'Escape') { el.textContent = origName; } });
      input.addEventListener('blur', save);
    }

    async function deleteProjectConfirm(projectId, name) {
      if (!confirm(`Delete "${name}" and all its entries? This cannot be undone.`)) return;
      try {
        const resp = await fetch(`/api/projects/${projectId}`, { method: 'DELETE' });
        if (!resp.ok) { const e = await resp.json(); throw new Error(e.error); }
        _cachedProjectList = null;
        projectsLoaded = false;
        overviewLoaded = false;
        await loadProjects();
      } catch (err) { alert('Delete failed: ' + err.message); }
    }

    async function createProjectInline() {
      const wrap = document.getElementById('create-project-form');
      if (wrap && !wrap.classList.contains('hidden')) { wrap.classList.add('hidden'); return; }
      if (wrap) { wrap.classList.remove('hidden'); wrap.querySelector('input')?.focus(); return; }
    }

    async function submitCreateProject() {
      const nameInput = document.getElementById('new-project-name');
      const tagSelect = document.getElementById('new-project-tag');
      const name = (nameInput?.value || '').trim();
      if (!name) return;
      const tag = tagSelect?.value || null;
      try {
        const resp = await fetch('/api/projects/create', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, tag: tag || undefined }),
        });
        if (!resp.ok) { const e = await resp.json(); throw new Error(e.error); }
        _cachedProjectList = null;
        projectsLoaded = false;
        await loadProjects();
      } catch (err) { alert('Create failed: ' + err.message); }
    }

    // ── Init ───────────────────────────────────────────────────────────

    async function init() {
      try {
        const resp = await fetch('/api/days');
        if (!resp.ok) throw new Error(`/api/days returned ${resp.status}`);
        const data = await resp.json();

        // Sort ascending so index 0 = oldest, last = newest
        availableDays = (data.days || []).slice().sort();

        if (availableDays.length > 0) {
          // Default to most recent day (used when user switches to Daily View)
          currentDate = availableDays[availableDays.length - 1];
        } else {
          currentDate = new Date().toISOString().slice(0, 10);
        }

        // Populate date picker min/max
        const picker = document.getElementById('date-picker');
        if (picker && availableDays.length > 0) {
          picker.min = availableDays[0];
          picker.max = availableDays[availableDays.length - 1];
        }

        renderDayStrip();
        updateDateDisplay();

        // Pre-fetch tag colors for create project dropdown
        if (!Object.keys(_shippedTagColors).length) {
          fetch('/api/tags').then(r => r.json()).then(d => { _shippedTagColors = d.tags || {}; }).catch(() => {});
        }

        switchTab(currentTab);

      } catch (err) {
        setError('summary-content',  `Initialisation failed: ${err.message}`);
        setError('timeline-content', 'Could not reach the DayView backend.');
        setError('meetings-content', 'Could not reach the DayView backend.');

        // Still show a date so the UI isn't blank
        currentDate = new Date().toISOString().slice(0, 10);
        updateDateDisplay();

        switchTab('daily');
      }
    }

    // ── Event listeners ────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', () => {
      // Tab switching
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
      });

      // Navigation arrows
      document.getElementById('btn-prev')
        ?.addEventListener('click', () => navigateDay(-1));
      document.getElementById('btn-next')
        ?.addEventListener('click', () => navigateDay(+1));

      // Keyboard left/right arrow keys
      document.addEventListener('keydown', e => {
        // Skip if focus is inside a text input
        const tag = document.activeElement?.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA') return;

        if (e.key === 'ArrowLeft')  navigateDay(-1);
        if (e.key === 'ArrowRight') navigateDay(+1);
      });

      // Click on date label opens the hidden date picker
      const dateDisplay = document.getElementById('date-display');
      const datePicker  = document.getElementById('date-picker');

      dateDisplay?.addEventListener('click',   () => datePicker?.showPicker?.());
      dateDisplay?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          datePicker?.showPicker?.();
        }
      });

      datePicker?.addEventListener('change', e => {
        const picked = e.target.value;
        if (picked && availableDays.includes(picked)) {
          loadDay(picked);
        } else if (picked) {
          // Date picked has no data — navigate to nearest available
          const sorted = [...availableDays].sort();
          const nearest = sorted.find(d => d >= picked) || sorted[sorted.length - 1];
          if (nearest) loadDay(nearest);
        }
      });

      // Search
      document.getElementById('btn-search')
        ?.addEventListener('click', doSearch);

      document.getElementById('search-input')
        ?.addEventListener('keydown', e => {
          if (e.key === 'Enter') doSearch();
        });

      // Start app
      init();
    });
  
