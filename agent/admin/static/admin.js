document.addEventListener("DOMContentLoaded", () => {
  animateReveals();
  setupAutoRefresh();
  setupSecretReveal();
  setupLlmModelDiscovery();
  setupLlmModelTesting();
  setupGitlabProjectTargets();
  setupGitlabActions();
  setupDailyAuditActions();
  setupSelfEvolutionActions();
  setupActorControls();
});

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

      status.textContent = "正在拉取模型列表...";
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
          throw new Error(payload.error || "模型列表请求失败");
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
          ? `已拉取 ${payload.models.length} 个模型，可继续手动输入。`
          : "接口可达，但没有返回模型。你仍可手动输入模型名。";
        status.dataset.tone = payload.models.length ? "success" : "warning";
      } catch (error) {
        status.textContent = `${error.message} 你仍可手动输入模型名。`;
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
      status.textContent = "正在向模型发送真实测试请求...";
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
          throw new Error(payload.error || "模型测试失败");
        }

        status.textContent = `模型测试成功：${payload.model_id}`;
        status.dataset.tone = "success";
        output.hidden = false;
        output.textContent = payload.response_text || "(模型已响应，但没有返回可展示的文本内容。)";
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
    input.placeholder = "https://gitlab.example.com/group/project.git 或 group/project";
    input.spellcheck = false;
    input.dataset.gitlabProjectItem = "";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button";
    button.dataset.gitlabProjectRemove = "";
    button.textContent = "删除";

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
    summary.textContent = `${actionLabel}中...`;
    summary.dataset.tone = "info";

    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || `${actionLabel}失败`);
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
    runAction("/admin/api/gitlab/verify", verifyButton, "验证 GitLab 连接");
  });
  syncButton.addEventListener("click", () => {
    runAction("/admin/api/gitlab/webhooks/sync", syncButton, "同步 Webhook");
  });
}

function setupDailyAuditActions() {
  const triggerButton = document.querySelector('[data-daily-audit-action="trigger"]');
  const status = document.querySelector('[data-daily-audit-status="summary"]');
  if (!triggerButton || !status) {
    return;
  }

  triggerButton.addEventListener("click", async () => {
    triggerButton.disabled = true;
    status.textContent = "正在触发日常审计...";
    status.dataset.tone = "info";

    try {
      const response = await fetch("/admin/api/daily-audit/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
      });
      const payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || "触发日常审计失败");
      }

      const targets = (payload.results || []).map((item) => item.project_id).join(", ");
      status.textContent = payload.scheduled_count
        ? `已触发 ${payload.scheduled_count} 个项目：${targets}`
        : "未触发任何项目。";
      status.dataset.tone = payload.scheduled_count ? "success" : "warning";
    } catch (error) {
      status.textContent = error.message;
      status.dataset.tone = "danger";
    } finally {
      triggerButton.disabled = false;
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
      const agentType = button.dataset.selfEvolutionAgent || "";
      const status = document.querySelector(`[data-self-evolution-status="${agentType}"]`);
      if (!agentType || !status) {
        return;
      }

      button.disabled = true;
      status.textContent = `正在触发 ${agentType} 自我演进...`;
      status.dataset.tone = "info";

      try {
        const response = await fetch("/admin/api/self-evolution/trigger", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ agent_type: agentType }),
        });
        const payload = await response.json();
        if (!response.ok || payload.error) {
          throw new Error(payload.error || "触发自我演进失败");
        }

        const targets = (payload.results || []).map((item) => item.project_id).join(", ");
        status.textContent = payload.scheduled_count
          ? `已触发 ${payload.scheduled_count} 个项目：${targets}`
          : "未触发任何项目。";
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
        throw new Error(payload.error || "操作失败");
      }
      const cancelledCount = Array.isArray(payload.cancelled_pending_events)
        ? payload.cancelled_pending_events.length
        : 0;
      status.textContent = cancelledCount > 0
        ? `${successLabel} 已同时取消 ${cancelledCount} 个排队任务。`
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
        "正在取消排队任务...",
        "已取消排队任务，正在刷新...",
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
        "正在终止当前运行...",
        "已发送终止请求，正在刷新...",
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
    ready: "GitLab 配置已通过验证。",
    ok: "Webhook 同步完成。",
    partial: "GitLab 配置部分可用，请处理告警项目。",
    invalid: "GitLab 配置还不可用。",
    degraded: "GitLab 配置存在风险，请检查告警。",
  };
  summary.textContent = labels[status] || `状态：${status}`;
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
