/* Factory AI Native OS — Shared Shell (Single-line Navigation, Theme Toggle, Responsive Tables) */
(function () {
  "use strict";

  const THEME_KEY = "fos_theme";
  const DOMAINS = [
    {
      title: "核心生产",
      items: [
        { href: "index.html", file: "index.html", label: "文档解析", desc: "OCR 与字段抽取核对" },
        { href: "eng.html", file: "eng.html", label: "工程方案", desc: "窑衬设计与热工计算" },
        { href: "kiln.html", file: "kiln.html", label: "窑炉监控", desc: "实时测点、曲线与告警" },
        { href: "plan.html", file: "plan.html", label: "计划排窑", desc: "订单排程与窑炉甘特图" },
      ],
    },
    {
      title: "供应链与招采",
      items: [
        { href: "tender.html", file: "tender.html", label: "招标拆解", desc: "标书条款与星标审查" },
        { href: "supply.html", file: "supply.html", label: "采购仓储", desc: "原料库存与采购协同" },
        { href: "quality.html", file: "quality.html", label: "质量体系", desc: "质检放行与批次追溯" },
        { href: "rnd.html", file: "rnd.html", label: "研发配方", desc: "配方迭代与试产记录" },
      ],
    },
    {
      title: "厂务与工程",
      items: [
        { href: "construction.html", file: "construction.html", label: "筑炉施工", desc: "施工进度与烘炉验收" },
        { href: "eam.html", file: "eam.html", label: "设备管理", desc: "可靠性指标与保养计划" },
        { href: "ehs.html", file: "ehs.html", label: "安全环保", desc: "隐患闭环与合规巡检" },
        { href: "pilot.html", file: "pilot.html", label: "试点上线", desc: "上线验收与模拟演练" },
      ],
    },
    {
      title: "经营与治理",
      items: [
        { href: "finance.html", file: "finance.html", label: "财务成本", desc: "成本核算与毛利分析" },
        { href: "hr.html", file: "hr.html", label: "人力薪酬", desc: "班组排班与计件薪酬" },
        { href: "market.html", file: "market.html", label: "经营与市场", desc: "经营驾驶舱与商机月报" },
        { href: "ops.html", file: "ops.html", label: "运营总控", desc: "多租户治理与自检审计" },
      ],
    },
  ];

  const QUICK_LINKS = [
    { href: "index.html", file: "index.html", label: "文档解析" },
    { href: "eng.html", file: "eng.html", label: "工程方案" },
    { href: "kiln.html", file: "kiln.html", label: "窑炉监控" },
    { href: "plan.html", file: "plan.html", label: "计划排窑" },
    { href: "ops.html", file: "ops.html", label: "运营总控" },
  ];

  function currentFile() {
    const seg = (window.location.pathname || "").split("/").pop();
    return seg && seg.endsWith(".html") ? seg : "index.html";
  }

  function applySavedTheme() {
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "dark" || saved === "light") {
        document.documentElement.dataset.theme = saved;
      }
    } catch (_) {}
  }

  function effectiveTheme() {
    const explicit = document.documentElement.dataset.theme;
    if (explicit === "dark" || explicit === "light") return explicit;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function createThemeButton() {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-btn";
    btn.setAttribute("aria-label", "切换深浅色主题");
    const sync = () => {
      const isDark = effectiveTheme() === "dark";
      btn.textContent = isDark ? "浅色" : "深色";
      btn.title = isDark ? "切换为浅色工作台" : "切换为深色值班模式";
    };
    sync();
    btn.addEventListener("click", () => {
      const next = effectiveTheme() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (_) {}
      sync();
    });
    return btn;
  }

  function buildModuleSwitcher(activeFile) {
    const wrap = document.createElement("div");
    wrap.className = "nav-menu-wrap";

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "nav-trigger";
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-haspopup", "true");
    trigger.textContent = "全部工作台 ▾";

    const pop = document.createElement("div");
    pop.className = "nav-popover";
    pop.hidden = true;
    pop.setAttribute("role", "menu");

    for (const domain of DOMAINS) {
      const sec = document.createElement("div");
      const h = document.createElement("div");
      h.className = "nav-group-title";
      h.textContent = domain.title;
      sec.appendChild(h);

      const grid = document.createElement("div");
      grid.className = "nav-group-grid";
      for (const item of domain.items) {
        const a = document.createElement("a");
        a.className = "nav-item";
        a.href = item.href;
        if (item.file === activeFile) a.setAttribute("aria-current", "page");

        const t = document.createElement("span");
        t.className = "ni-title";
        t.textContent = item.label;

        const d = document.createElement("span");
        d.className = "ni-desc";
        d.textContent = item.desc;

        a.appendChild(t);
        a.appendChild(d);
        grid.appendChild(a);
      }
      sec.appendChild(grid);
      pop.appendChild(sec);
    }

    const foot = document.createElement("div");
    foot.className = "nav-popover-foot";
    const info = document.createElement("span");
    info.textContent = "Factory AI Native OS · 全厂 16 个工作台";
    const loginLink = document.createElement("a");
    loginLink.href = "login.html";
    loginLink.textContent = "统一登录与会话管理";
    foot.appendChild(info);
    foot.appendChild(loginLink);
    pop.appendChild(foot);

    function setOpen(open) {
      pop.hidden = !open;
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    }

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(pop.hidden);
    });
    document.addEventListener("click", (e) => {
      if (!pop.hidden && !wrap.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !pop.hidden) {
        setOpen(false);
        trigger.focus();
      }
    });

    wrap.appendChild(trigger);
    wrap.appendChild(pop);
    return wrap;
  }

  function enhanceHeader() {
    const active = currentFile();
    if (active === "login.html") return;

    const topHeader = document.querySelector("header.top");
    const barHeader = document.querySelector("header > .bar");
    if (!topHeader && !barHeader) return;

    // Build concise single-line quick nav
    const nav = document.createElement("nav");
    nav.className = "fos-nav";
    nav.setAttribute("aria-label", "核心工作台");
    for (const q of QUICK_LINKS) {
      const a = document.createElement("a");
      a.className = "fos-nav-link";
      a.href = q.href;
      a.textContent = q.label;
      if (q.file === active) a.setAttribute("aria-current", "page");
      nav.appendChild(a);
    }

    const switcher = buildModuleSwitcher(active);
    const themeBtn = createThemeButton();

    if (topHeader) {
      const right = topHeader.querySelector(".top-right");
      if (!right) return;
      // Remove legacy wall of page-link <a class="btn small"> while preserving #modes, #b-trace, #who, #logout
      right.querySelectorAll("a.btn").forEach((a) => a.remove());
      const firstChild = right.firstChild;
      right.insertBefore(nav, firstChild);
      right.insertBefore(switcher, firstChild);
      right.appendChild(themeBtn);
    } else if (barHeader) {
      const who = barHeader.querySelector("#who");
      const titleMatch = DOMAINS.flatMap((d) => d.items).find((i) => i.file === active);
      const moduleLabel = titleMatch ? titleMatch.label : document.title.split("·")[0].trim();

      barHeader.innerHTML = "";
      barHeader.className = "top";
      const brand = document.createElement("div");
      brand.className = "brand";
      const logo = document.createElement("span");
      logo.className = "logo";
      logo.textContent = moduleLabel.slice(0, 1) || "工";
      const b = document.createElement("b");
      b.textContent = moduleLabel;
      const sub = document.createElement("span");
      sub.className = "muted";
      sub.textContent = "工厂 AI OS";
      brand.appendChild(logo);
      brand.appendChild(b);
      brand.appendChild(sub);

      const right = document.createElement("div");
      right.className = "top-right";
      right.appendChild(nav);
      right.appendChild(switcher);
      if (who) {
        who.style.marginLeft = "";
        right.appendChild(who);
      }
      right.appendChild(themeBtn);

      barHeader.appendChild(brand);
      barHeader.appendChild(right);
    }
  }

  function wrapTables() {
    document.querySelectorAll("main table").forEach((tbl) => {
      if (tbl.closest(".table-wrap, .scroll")) return;
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      tbl.parentNode.insertBefore(wrap, tbl);
      wrap.appendChild(tbl);
    });
  }

  applySavedTheme();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      enhanceHeader();
      wrapTables();
    });
  } else {
    enhanceHeader();
    wrapTables();
  }
})();
