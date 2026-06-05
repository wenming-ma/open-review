document.addEventListener("DOMContentLoaded", () => {
  setupLanguageSwitcher();
  animateReveals();
  setupAutoRefresh();
  setupSecretReveal();
  setupLlmModelDiscovery();
  setupLlmModelTesting();
  setupGitlabProjectTargets();
  setupGitlabActions();
  setupProjectAgentSettings();
  setupDailyAuditActions();
  setupSelfEvolutionActions();
  setupActorControls();
});

const UI_TEXT_EN = {
  "正在拉取模型列表...": "Fetching model list...",
  "模型列表请求失败": "Model list request failed",
  "接口可达，但没有返回模型。你仍可手动输入模型名。": "The endpoint is reachable, but returned no models. You can still enter a model manually.",
  "你仍可手动输入模型名。": "You can still enter a model manually.",
  "正在向模型发送真实测试请求...": "Sending a real test request to the model...",
  "模型测试失败": "Model test failed",
  "(模型已响应，但没有返回可展示的文本内容。)": "(The model responded, but returned no displayable text.)",
  "https://gitlab.example.com/group/project.git 或 group/project": "https://gitlab.example.com/group/project.git or group/project",
  "删除": "Remove",
  "验证 GitLab 连接": "Verify GitLab connection",
  "同步 Webhook": "Sync Webhook",
  "已拉取 {count} 个模型，可继续手动输入。": "Fetched {count} models. Manual input is still allowed.",
  "模型测试成功：{model}": "Model test succeeded: {model}",
  "正在执行 {action}...": "{action}...",
  "{action}失败": "{action} failed",
  "正在触发日常审计...": "Triggering Daily Audit...",
  "触发日常审计失败": "Failed to trigger Daily Audit",
  "未触发任何项目。": "No projects were triggered.",
  "已触发 {count} 个项目：{targets}": "Triggered {count} projects: {targets}",
  "正在触发自我演进...": "Triggering self-evolution...",
  "触发自我演进失败": "Failed to trigger self-evolution",
  "操作失败": "Operation failed",
  "{successLabel} 已同时取消 {count} 个排队任务。": "{successLabel} Also cancelled {count} queued tasks.",
  "正在取消排队任务...": "Cancelling queued task...",
  "已取消排队任务，正在刷新...": "Queued task cancelled. Refreshing...",
  "正在终止当前运行...": "Terminating current run...",
  "已发送终止请求，正在刷新...": "Termination request sent. Refreshing...",
  "GitLab 配置已通过验证。": "GitLab configuration passed validation.",
  "Webhook 同步完成。": "Webhook sync completed.",
  "GitLab 配置部分可用，请处理告警项目。": "GitLab configuration is partially usable; handle the warnings.",
  "GitLab 配置还不可用。": "GitLab configuration is not ready.",
  "GitLab 配置存在风险，请检查告警。": "GitLab configuration has risks; check the warnings.",
  "状态：{status}": "Status: {status}",
};

function adminLang() {
  return document.body.dataset.adminLang || "zh";
}

function t(text) {
  if (adminLang() !== "en") {
    return text;
  }
  return UI_TEXT_EN[text] || text;
}

function tf(text, values) {
  return t(text).replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key) => (
    Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : match
  ));
}

function setupLanguageSwitcher() {
  document.querySelectorAll("[data-lang-next]").forEach((input) => {
    try {
      const current = new URL(window.location.href);
      current.searchParams.delete("lang");
      input.value = current.pathname + current.search + current.hash;
    } catch (_error) {
      input.value = window.location.pathname || "/admin";
    }
  });
}

function animateReveals() {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return;
  }

  document.querySelectorAll(".reveal").forEach((node, index) => {
    node.animate(
      [
        { opacity: 0 },
        { opacity: 1 },
      ],
      {
        duration: 140,
        delay: Math.min(index * 12, 60),
        easing: "ease-out",
        fill: "both",
      },
    );
  });
}

function setupAutoRefresh() {
  const intervalSeconds = Number(document.body.dataset.autorefresh || "0");
  const page = document.body.dataset.page || "";
  if (!intervalSeconds || !["dashboard", "actors", "actor-detail"].includes(page)) {
    return;
  }

  window.setInterval(() => {
    const active = document.activeElement;
    if (document.hidden || (active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName))) {
      return;
    }
    window.location.reload();
  }, intervalSeconds * 1000);
}

function setupSecretReveal() {
  const toggles = document.querySelectorAll("[data-secret-toggle]");
  if (!toggles.length) {
    return;
  }

  toggles.forEach((button) => {
    button.addEventListener("click", () => {
      const wrapper = button.closest(".secret-input");
      const input = wrapper ? wrapper.querySelector("[data-secret-input]") : null;
      if (!input) {
        return;
      }
      const visible = input.type === "text";
      input.type = visible ? "password" : "text";
      button.setAttribute("aria-pressed", visible ? "false" : "true");
      button.classList.toggle("is-active", !visible);
    });
  });
}

function setupLlmModelDiscovery() {
  const buttons = document.querySelectorAll("[data-model-refresh]");
  if (!buttons.length) {
    return;
  }

  document.querySelectorAll('input[name="LLM_ACTIVE_PROVIDER"]').forEach((radio) => {
    radio.addEventListener("change", syncProviderPanels);
  });
  syncProviderPanels();

  buttons.forEach((button) => {
    button.addEventListener("click", async () => {
      const provider = button.dataset.modelRefresh || "";
      const panel = button.closest("[data-provider-panel]");
      if (!provider || !panel) {
        return;
      }

      const baseUrlInput = panel.querySelector("[data-llm-base-url]");
      const apiKeyInput = panel.querySelector("[data-llm-api-key]");
      const modelInput = panel.querySelector("[data-llm-model-input]");
      const datalist = document.getElementById(`${provider}-model-list`);
      const status = panel.querySelector(`[data-llm-status="${provider}"]`);

      if (!baseUrlInput || !apiKeyInput || !modelInput || !datalist || !status) {
        return;
      }

      status.textContent = t("正在拉取模型列表...");
      status.dataset.tone = "info";

      try {
        const response = await fetch("/admin/api/llm/models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({
            provider,
            base_url: baseUrlInput.value,
            api_key: apiKeyInput.value,
          }),
        });
        const payload = await response.json();
        if (!response.ok || payload.error) {
          throw new Error(payload.error || t("模型列表请求失败"));
        }

        datalist.replaceChildren(
          ...payload.models.map((model) => {
            const option = document.createElement("option");
            option.value = model;
            return option;
          }),
        );

        if (payload.models.length && !modelInput.value) {
          modelInput.value = payload.models[0];
        }
        status.textContent = payload.models.length
          ? tf("已拉取 {count} 个模型，可继续手动输入。", { count: payload.models.length })
          : t("接口可达，但没有返回模型。你仍可手动输入模型名。");
        status.dataset.tone = payload.models.length ? "success" : "warning";
      } catch (error) {
        status.textContent = `${error.message} ${t("你仍可手动输入模型名。")}`;
        status.dataset.tone = "danger";
      }
    });
  });
}

function syncProviderPanels() {
  const active = document.querySelector('input[name="LLM_ACTIVE_PROVIDER"]:checked');
  const activeProvider = active ? active.value : "";
  document.querySelectorAll("[data-provider-panel]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.providerPanel === activeProvider);
  });
}

function setupLlmModelTesting() {
  const buttons = document.querySelectorAll("[data-llm-test]");
  if (!buttons.length) {
    return;
  }

  buttons.forEach((button) => {
    button.addEventListener("click", async () => {
      const provider = button.dataset.llmTest || "";
      const panel = button.closest("[data-provider-panel]");
      if (!provider || !panel) {
        return;
      }

      const baseUrlInput = panel.querySelector("[data-llm-base-url]");
      const apiKeyInput = panel.querySelector("[data-llm-api-key]");
      const modelInput = panel.querySelector("[data-llm-model-input]");
      const status = panel.querySelector(`[data-llm-status="${provider}"]`);
      const output = panel.querySelector(`[data-llm-test-output="${provider}"]`);
      if (!baseUrlInput || !apiKeyInput || !modelInput || !status || !output) {
        return;
      }

      button.disabled = true;
      status.textContent = t("正在向模型发送真实测试请求...");
      status.dataset.tone = "info";
      output.hidden = true;
      output.textContent = "";

      try {
        const response = await fetch("/admin/api/llm/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({
            provider,
            base_url: baseUrlInput.value,
            api_key: apiKeyInput.value,
            model: modelInput.value,
          }),
        });
        const payload = await response.json();
        if (!response.ok || payload.error) {
          throw new Error(payload.error || t("模型测试失败"));
        }

        status.textContent = tf("模型测试成功：{model}", { model: payload.model_id });
        status.dataset.tone = "success";
        output.hidden = false;
        output.textContent = payload.response_text || t("(模型已响应，但没有返回可展示的文本内容。)");
      } catch (error) {
        status.textContent = error.message;
        status.dataset.tone = "danger";
        output.hidden = true;
        output.textContent = "";
      } finally {
        button.disabled = false;
      }
    });
  });
}

function setupGitlabProjectTargets() {
  const container = document.querySelector("[data-gitlab-project-targets]");
  const list = document.querySelector("[data-gitlab-project-list]");
  const hiddenInput = document.querySelector("[data-gitlab-targets-input]");
  const addButton = document.querySelector("[data-gitlab-project-add]");
  if (!container || !list || !hiddenInput || !addButton) {
    return;
  }

  function syncHiddenInput() {
    hiddenInput.value = Array.from(list.querySelectorAll("[data-gitlab-project-item]"))
      .map((input) => input.value.trim())
      .filter(Boolean)
      .join("\n");
  }

  function ensureAtLeastOneRow() {
    if (list.querySelector("[data-gitlab-project-row]")) {
      return;
    }
    appendRow("");
  }

  function appendRow(value) {
    const row = document.createElement("div");
    row.className = "gitlab-project-row";
    row.dataset.gitlabProjectRow = "";

    const input = document.createElement("input");
    input.type = "text";
    input.value = value;
    input.placeholder = t("https://gitlab.example.com/group/project.git 或 group/project");
    input.spellcheck = false;
    input.dataset.gitlabProjectItem = "";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button";
    button.dataset.gitlabProjectRemove = "";
    button.textContent = t("删除");

    row.append(input, button);
    list.append(row);
    syncHiddenInput();
    return input;
  }

  addButton.addEventListener("click", () => {
    const input = appendRow("");
    input.focus();
  });

  list.addEventListener("input", (event) => {
    if (!(event.target instanceof HTMLInputElement) || !event.target.hasAttribute("data-gitlab-project-item")) {
      return;
    }
    syncHiddenInput();
  });

  list.addEventListener("click", (event) => {
    const button = event.target instanceof Element
      ? event.target.closest("[data-gitlab-project-remove]")
      : null;
    if (!button) {
      return;
    }
    const row = button.closest("[data-gitlab-project-row]");
    if (row) {
      row.remove();
      ensureAtLeastOneRow();
      syncHiddenInput();
    }
  });

  const form = container.closest("form");
  if (form) {
    form.addEventListener("submit", syncHiddenInput);
  }

  ensureAtLeastOneRow();
  syncHiddenInput();
}

function setupGitlabActions() {
  const verifyButton = document.querySelector('[data-gitlab-action="verify"]');
  const syncButton = document.querySelector('[data-gitlab-action="sync"]');
  const summary = document.querySelector('[data-gitlab-status="summary"]');
  const checklist = document.querySelector("[data-gitlab-checklist]");
  const results = document.querySelector("[data-gitlab-results]");
  if (!verifyButton || !syncButton || !summary || !checklist || !results) {
    return;
  }

  async function runAction(url, button, actionLabel) {
    verifyButton.disabled = true;
    syncButton.disabled = true;
    summary.textContent = tf("正在执行 {action}...", { action: actionLabel });
    summary.dataset.tone = "info";

    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || tf("{action}失败", { action: actionLabel }));
      }
      renderGitlabPayload(payload, summary, checklist, results);
    } catch (error) {
      summary.textContent = error.message;
      summary.dataset.tone = "danger";
      checklist.replaceChildren();
      results.replaceChildren();
    } finally {
      verifyButton.disabled = false;
      syncButton.disabled = false;
    }
  }

  verifyButton.addEventListener("click", () => {
    runAction("/admin/api/gitlab/verify", verifyButton, t("验证 GitLab 连接"));
  });
  syncButton.addEventListener("click", () => {
    runAction("/admin/api/gitlab/webhooks/sync", syncButton, t("同步 Webhook"));
  });
}

function setupDailyAuditActions() {
  const triggerButtons = document.querySelectorAll('[data-daily-audit-action="trigger"]');
  if (!triggerButtons.length) {
    return;
  }

  triggerButtons.forEach((triggerButton) => {
    triggerButton.addEventListener("click", async () => {
      const projectId = triggerButton.dataset.dailyAuditProject || "";
      const section = triggerButton.closest("[data-agent-section]");
      const status = section
        ? section.querySelector("[data-daily-audit-status]")
        : document.querySelector('[data-daily-audit-status="summary"]');
      if (!status) {
        return;
      }

      triggerButton.disabled = true;
      status.textContent = t("正在触发日常审计...");
      status.dataset.tone = "info";

      try {
        const body = projectId ? JSON.stringify({ project_id: projectId }) : undefined;
        const response = await fetch("/admin/api/daily-audit/trigger", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body,
        });
        const payload = await response.json();
        if (!response.ok || payload.error) {
          throw new Error(payload.error || t("触发日常审计失败"));
        }

        const targets = (payload.results || []).map((item) => item.project_id).join(", ");
        status.textContent = payload.scheduled_count
          ? tf("已触发 {count} 个项目：{targets}", { count: payload.scheduled_count, targets })
          : t("未触发任何项目。");
        status.dataset.tone = payload.scheduled_count ? "success" : "warning";
      } catch (error) {
        status.textContent = error.message;
        status.dataset.tone = "danger";
      } finally {
        triggerButton.disabled = false;
      }
    });
  });
}

function setupProjectAgentSettings() {
  document.querySelectorAll("[data-project-agent-workspace]").forEach((workspace) => {
    const tabs = Array.from(workspace.querySelectorAll("[data-agent-project-tab]"));
    const panels = Array.from(workspace.querySelectorAll("[data-agent-project-panel]"));
    if (!tabs.length || !panels.length) {
      return;
    }

    const storageKey = "open-review-admin-agent-project";

    function activate(projectId) {
      const fallback = tabs[0] ? tabs[0].dataset.agentProjectTab : "";
      const selected = projectId && panels.some((panel) => panel.dataset.agentProjectPanel === projectId)
        ? projectId
        : fallback;
      if (!selected) {
        return;
      }

      tabs.forEach((tab) => {
        const active = tab.dataset.agentProjectTab === selected;
        tab.classList.toggle("is-active", active);
        tab.setAttribute("aria-selected", active ? "true" : "false");
      });

      panels.forEach((panel) => {
        const active = panel.dataset.agentProjectPanel === selected;
        panel.classList.toggle("is-active", active);
        panel.hidden = !active;
      });

      try {
        window.localStorage.setItem(storageKey, selected);
      } catch (_error) {
        // Ignore storage failures in private browsing or locked-down environments.
      }
    }

    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        activate(tab.dataset.agentProjectTab || "");
      });
    });

    try {
      activate(window.localStorage.getItem(storageKey) || "");
    } catch (_error) {
      activate("");
    }
  });
}

function setupSelfEvolutionActions() {
  const buttons = document.querySelectorAll('[data-self-evolution-action="trigger"]');
  if (!buttons.length) {
    return;
  }

  buttons.forEach((button) => {
    button.addEventListener("click", async () => {
      const status = document.querySelector('[data-self-evolution-status="global"]');
      if (!status) {
        return;
      }

      button.disabled = true;
      status.textContent = t("正在触发自我演进...");
      status.dataset.tone = "info";

      try {
        const response = await fetch("/admin/api/self-evolution/trigger", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({}),
        });
        const payload = await response.json();
        if (!response.ok || payload.error) {
          throw new Error(payload.error || t("触发自我演进失败"));
        }

        const targets = (payload.results || []).map((item) => item.project_id).join(", ");
        status.textContent = payload.scheduled_count
          ? tf("已触发 {count} 个项目：{targets}", { count: payload.scheduled_count, targets })
          : t("未触发任何项目。");
        status.dataset.tone = payload.scheduled_count ? "success" : "warning";
      } catch (error) {
        status.textContent = error.message;
        status.dataset.tone = "danger";
      } finally {
        button.disabled = false;
      }
    });
  });
}

function setupActorControls() {
  const actorKey = document.body.dataset.actorKey || "";
  const status = document.querySelector("[data-actor-control-status]");
  const pendingButtons = document.querySelectorAll("[data-actor-pending-cancel]");
  const terminateButtons = document.querySelectorAll("[data-actor-run-terminate]");
  if (!actorKey || (!pendingButtons.length && !terminateButtons.length) || !status) {
    return;
  }

  async function runAction(button, url, loadingLabel, successLabel) {
    button.disabled = true;
    status.textContent = loadingLabel;
    status.dataset.tone = "info";
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
      });
      const payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || t("操作失败"));
      }
      const cancelledCount = Array.isArray(payload.cancelled_pending_events)
        ? payload.cancelled_pending_events.length
        : 0;
      status.textContent = cancelledCount > 0
        ? tf("{successLabel} 已同时取消 {count} 个排队任务。", { successLabel, count: cancelledCount })
        : successLabel;
      status.dataset.tone = "success";
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      status.dataset.tone = "danger";
      button.disabled = false;
    }
  }

  pendingButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const eventId = button.dataset.eventId || "";
      if (!eventId) {
        return;
      }
      runAction(
        button,
        `/admin/api/actors/${encodeURIComponent(actorKey)}/pending/${encodeURIComponent(eventId)}/cancel`,
        t("正在取消排队任务..."),
        t("已取消排队任务，正在刷新..."),
      );
    });
  });

  terminateButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const runId = button.dataset.runId || "";
      if (!runId) {
        return;
      }
      runAction(
        button,
        `/admin/api/actors/${encodeURIComponent(actorKey)}/runs/${encodeURIComponent(runId)}/terminate`,
        t("正在终止当前运行..."),
        t("已发送终止请求，正在刷新..."),
      );
    });
  });
}

function renderGitlabPayload(payload, summary, checklist, results) {
  const status = payload.status || "invalid";
  const tone = status === "ready" || status === "ok"
    ? "success"
    : status === "partial"
      ? "warning"
      : "danger";
  const labels = {
    ready: t("GitLab 配置已通过验证。"),
    ok: t("Webhook 同步完成。"),
    partial: t("GitLab 配置部分可用，请处理告警项目。"),
    invalid: t("GitLab 配置还不可用。"),
    degraded: t("GitLab 配置存在风险，请检查告警。"),
  };
  summary.textContent = labels[status] || tf("状态：{status}", { status });
  summary.dataset.tone = tone;

  checklist.replaceChildren(
    ...((payload.checks || []).map((item) => {
      const row = document.createElement("div");
      row.className = "gitlab-check-item";
      row.dataset.tone = item.status || "info";
      row.textContent = `${item.key}: ${item.message}`;
      return row;
    })),
  );

  const nodes = [];
  (payload.results || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = "gitlab-check-item";
    row.dataset.tone = item.status === "error" ? "danger" : item.status === "updated" || item.status === "created" ? "success" : "info";
    row.textContent = `${item.project_path}: ${item.detail}`;
    nodes.push(row);
  });
  if (payload.manual_instructions) {
    const note = document.createElement("div");
    note.className = "gitlab-manual-note";
    note.textContent = payload.manual_instructions;
    nodes.push(note);
  }
  results.replaceChildren(...nodes);
}
