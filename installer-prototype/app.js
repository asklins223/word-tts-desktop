(() => {
  'use strict';

  const runtime = window.installerAPI || null;
  const previewMode = new URLSearchParams(window.location.search).get('mode');

  function versionLabel(value, fallback = '—') {
    const text = String(value || '').trim();
    if (!text) return fallback;
    return /^v/i.test(text) || !/^\d/.test(text) ? text : `v${text}`;
  }

  function createModeConfig(version, installedVersion) {
    const target = versionLabel(version);
    const current = versionLabel(installedVersion, '已安装');
    const currentDisplay = /^v?\d/i.test(current) ? current : '旧版本';
    return {
      install: {
        label: '安装',
        stepLabels: ['欢迎', '选范围', '选住址', '安装', '完成'],
        flow: ['welcome', 'scope', 'location', 'confirm', 'progress', 'finish'],
        screenIndexes: { welcome: 0, scope: 1, location: 2, confirm: 3, progress: 3, finish: 4 },
        welcome: {
          title: '小猪准备入住啦',
          description: '这台电脑还没找到小猪wordTTS，跟着我把它安顿好，几步就能开工。',
          facts: [
            ['当前状况', '还没安装'],
            ['这次要做', '把小猪安顿好'],
            ['放心事项', '你的文档不动'],
          ],
        },
        welcomeNote: { icon: 'icon-file', text: '已经替你判断好接下来该走哪条路，跟着小猪走就行。' },
        screenCopy: {
          scope: ['小猪住哪一户？', '先决定是只给你用，还是让这台电脑上的每个账户都能喊小猪出来干活。'],
          location: ['给小猪挑个窝', '选个空闲文件夹放程序文件，住址以后也能在设置里查到。'],
          confirm: ['行李清单对一下', '确认无误就开搬：写入程序文件，再把快捷方式安排上。'],
        },
        progress: {
          title: '小猪正在搬家…',
          description: '先别关窗，小猪还在认真搬行李。',
          cancel: '先暂停搬家',
          phases: ['整理行李', '搬进小窝', '留好入口', '关门营业'],
          entries: [
            { limit: 24, phase: 'prepare', stage: '正在整理应用行李', file: '正在检查安装包…', count: '准备中' },
            { limit: 72, phase: 'write', stage: '正在把应用搬进小窝', file: '正在展开小猪wordTTS.exe…', count: value => `${Math.max(1, Math.round(value * 1.8))} 个文件` },
            { limit: 94, phase: 'shortcut', stage: '正在给小猪留入口', file: '正在写入开始菜单和桌面快捷方式…', count: '最后一步' },
            { limit: 101, phase: 'finish', stage: '正在关门营业', file: '正在保存入住信息…', count: '即将完成' },
          ],
        },
        finish: {
          title: '入住成功，开门营业！',
          description: '小猪wordTTS 已经准备好，随时可以开始工作。',
          summary: '程序文件和快捷方式都安排好了',
          version: target,
          launch: true,
        },
      },
      update: {
        label: '更新',
        stepLabels: ['欢迎', '看版本', '留东西', '更新', '完成'],
        flow: ['welcome', 'update-check', 'update-options', 'update-confirm', 'progress', 'finish'],
        screenIndexes: { welcome: 0, 'update-check': 1, 'update-options': 2, 'update-confirm': 3, progress: 3, finish: 4 },
        welcome: {
          title: '小猪要换新外套啦',
          description: `旧版本${currentDisplay === '旧版本' ? '' : ` ${currentDisplay}`}我看到了，新版本 ${target} 也到位，设置和历史任务都给你留着。`,
          facts: [
            ['当前状况', currentDisplay === '旧版本' ? '已经找到旧版本' : `已经是 ${currentDisplay}`],
            ['这次要做', '换上新外套'],
            ['放心事项', '设置继续在岗'],
          ],
        },
        welcomeNote: { icon: 'icon-refresh', text: '已经替你识别到旧版本，接下来只换程序外套，不动你的文档。' },
        screenCopy: {
          'update-check': ['先看看要换哪一版', '旧版本在这儿，新版本也到位。确认一下，马上给小猪换件新外套。'],
          'update-options': ['该留的都留着', '应用文件会换成新的，个人设置和历史任务会自动保留；下面只调整快捷方式入口。'],
          'update-confirm': ['确认给小猪换装', '点下确认，旧外套退场，新外套上身，设置和历史任务继续营业。'],
        },
        progress: {
          title: '小猪正在换装…',
          description: '先别关窗，小猪正在换新外套，马上就精神了。',
          cancel: '先暂停换装',
          phases: ['准备新衣', '换上外套', '更新入口', '焕新完成'],
          entries: [
            { limit: 24, phase: 'prepare', stage: '正在准备新外套', file: '正在校验新版本安装包…', count: '准备中' },
            { limit: 72, phase: 'write', stage: '正在替换应用外套', file: '正在更新小猪wordTTS.exe…', count: value => `${Math.max(1, Math.round(value * 1.8))} 个文件` },
            { limit: 94, phase: 'shortcut', stage: '正在更新快捷方式入口', file: '正在让桌面和开始菜单指向新版本…', count: '最后一步' },
            { limit: 101, phase: 'finish', stage: '正在整理新衣柜', file: '正在保存更新信息…', count: '即将完成' },
          ],
        },
        finish: {
          title: '换装完成，精神多了！',
          description: '小猪wordTTS 已经穿上新外套，个人设置照旧上班。',
          summary: '应用已更新，设置和历史任务都保留了',
          version: target,
          launch: true,
        },
      },
      uninstall: {
        label: '卸载',
        stepLabels: ['欢迎', '留东西', '再确认', '卸载', '完成'],
        flow: ['welcome', 'uninstall-options', 'uninstall-confirm', 'progress', 'finish'],
        screenIndexes: { welcome: 0, 'uninstall-options': 1, 'uninstall-confirm': 2, progress: 3, finish: 4 },
        welcome: {
          title: '小猪准备退租',
          description: `已经找到${currentDisplay === '旧版本' ? '' : ` ${currentDisplay}`}。程序文件会搬走，原始 Word / Excel 文档不会跟着跑路。`,
          facts: [
            ['当前状况', currentDisplay === '旧版本' ? '已经找到旧版本' : `已经是 ${currentDisplay}`],
            ['这次要做', '收拾程序行李'],
            ['默认留下', '你的原始文档'],
          ],
        },
        welcomeNote: { icon: 'icon-trash', text: '已经替你找到安装位置，接下来只清理程序，不碰原始文档。' },
        screenCopy: {
          'uninstall-options': ['哪些东西留下？', '小猪要退租了。程序文件会搬走，原始 Word / Excel 文档不会跟着跑路。'],
          'uninstall-confirm': ['最后确认一下', '再看一眼清单，点下开始后，小猪就要拎包出门啦。'],
        },
        progress: {
          title: '小猪正在收拾行李…',
          description: '先别关窗，小猪正在把程序行李一件件带走。',
          cancel: '先暂停收拾',
          phases: ['整理行李', '搬出小窝', '清理入口', '挥手告别'],
          entries: [
            { limit: 24, phase: 'prepare', stage: '正在整理程序行李', file: '正在检查应用文件…', count: '准备中' },
            { limit: 72, phase: 'write', stage: '正在把程序搬出去', file: '正在清理小猪wordTTS.exe…', count: value => `${Math.max(1, Math.round(value * 1.2))} 个文件` },
            { limit: 94, phase: 'shortcut', stage: '正在清理入口', file: '正在移除开始菜单和桌面快捷方式…', count: '最后一步' },
            { limit: 101, phase: 'finish', stage: '正在挥手告别', file: '正在清理安装信息…', count: '即将完成' },
          ],
        },
        finish: {
          title: '退租完成，后会有期！',
          description: '小猪wordTTS 已从这台电脑离开，留下的文档和数据按你的选择保管好啦。',
          summary: '程序文件和快捷方式都清理干净了',
          version: '已完成',
          launch: false,
        },
      },
    };
  }

  let runtimeConfig = {
    version: '',
    installedVersion: '已安装',
    mode: 'install',
    fixedMode: false,
    autoStart: false,
    targetPath: 'C:\\Users\\当前用户\\AppData\\Local\\小猪wordTTS',
    scope: 'per-user',
    defaultTargetPaths: {
      perUser: 'C:\\Users\\当前用户\\AppData\\Local\\小猪wordTTS',
      perMachine: 'C:\\Program Files\\小猪wordTTS',
    },
    shortcuts: { desktop: true, startMenu: true },
    allowedModes: ['install', 'update', 'uninstall'],
  };
  let modeConfig = createModeConfig(runtimeConfig.version, runtimeConfig.installedVersion);

  const state = {
    mode: 'install',
    screen: 'welcome',
    scope: 'per-user',
    path: runtimeConfig.targetPath,
    pathTouched: false,
    progress: 0,
    timer: null,
    completionTimer: null,
    toastTimer: null,
    operationRunning: false,
    operationError: null,
    operationHandled: false,
  };

  const screens = [...document.querySelectorAll('[data-screen]')];
  const steps = [...document.querySelectorAll('[data-step-index]')];
  const artwork = document.querySelector('#brand-artwork-image');
  const primaryAction = document.querySelector('#primary-action');
  const secondaryAction = document.querySelector('#secondary-action');
  const actionHint = document.querySelector('#action-hint');
  const pathInput = document.querySelector('#install-path');
  const summaryPath = document.querySelector('#summary-path');
  const summaryScope = document.querySelector('#summary-scope');
  const summaryUpdateData = document.querySelector('#summary-update-data');
  const summaryUninstallData = document.querySelector('#summary-uninstall-data');
  const modeNote = document.querySelector('#mode-note');
  const welcomeDescription = document.querySelector('#welcome-description');
  const welcomeFactElements = [
    [document.querySelector('#welcome-fact-one-label'), document.querySelector('#welcome-fact-one-value')],
    [document.querySelector('#welcome-fact-two-label'), document.querySelector('#welcome-fact-two-value')],
    [document.querySelector('#welcome-fact-three-label'), document.querySelector('#welcome-fact-three-value')],
  ];
  const toast = document.querySelector('#toast');
  const progressTitle = document.querySelector('#progress-title');
  const progressDescription = document.querySelector('#progress-description');
  const progressFill = document.querySelector('#progress-fill');
  const progressPercent = document.querySelector('#progress-percent');
  const progressStage = document.querySelector('#progress-stage');
  const progressFile = document.querySelector('#progress-file');
  const progressCount = document.querySelector('#progress-count');
  const progressTrack = document.querySelector('.progress-track');
  const phases = [...document.querySelectorAll('[data-phase]')];
  const finishTitle = document.querySelector('#finish-title');
  const finishDescription = document.querySelector('#finish-description');
  const finishSummary = document.querySelector('#finish-summary');
  const finishSummaryText = document.querySelector('#finish-summary-text');
  const finishVersion = document.querySelector('#finish-version');
  const launchOption = document.querySelector('.launch-option');
  const launchAfterFinish = document.querySelector('#launch-after-finish');
  const launchLabel = document.querySelector('#launch-label');
  const operationError = document.querySelector('#operation-error');
  const operationErrorMessage = document.querySelector('#operation-error-message');

  const modeText = {
    install: '检测到还没安装，准备把小猪接进家门。',
    update: '检测到已有版本，准备给小猪换件新外套。',
    uninstall: '检测到已有安装，准备把小猪安全送出门。',
  };

  const actionCopy = {
    scope: { hint: '选错了也没关系，上一页可以重来。', secondary: '上一步', primary: '继续选', icon: true },
    location: { hint: '安装器会替你看一眼磁盘空间。', secondary: '上一步', primary: '住这儿', icon: true },
    'update-check': { hint: '新旧版本排排站，看清楚再换。', secondary: '上一步', primary: '看明白了', icon: true },
    'update-options': { hint: '该留下的东西，给它们留个位置。', secondary: '上一步', primary: '继续换装', icon: true },
    'update-confirm': { hint: '确认后就开始替换应用文件。', secondary: '上一步', primary: '开始换装', icon: true },
    confirm: { hint: '确认后就开始搬入应用文件。', secondary: '上一步', primary: '开始入住', icon: true },
    'uninstall-options': { hint: '舍不得的东西可以留下。', secondary: '上一步', primary: '继续收拾', icon: true },
    'uninstall-confirm': { hint: '确认后会移除应用文件和快捷方式。', secondary: '上一步', primary: '送小猪出门', icon: true },
  };

  function currentConfig() {
    return modeConfig[state.mode];
  }

  function currentFlow() {
    return currentConfig().flow;
  }

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('is-visible');
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => toast.classList.remove('is-visible'), 3200);
  }

  function setText(selector, value) {
    const element = document.querySelector(selector);
    if (element) element.textContent = String(value ?? '');
  }

  function syncWelcomeCopy() {
    const welcome = currentConfig().welcome;
    if (!welcome) return;
    setText('#welcome-title', welcome.title);
    if (welcomeDescription) welcomeDescription.textContent = welcome.description;
    welcomeFactElements.forEach(([label, value], index) => {
      const fact = welcome.facts?.[index];
      if (label) label.textContent = fact?.[0] || '';
      if (value) value.textContent = fact?.[1] || '';
    });
  }

  function syncScreenCopy(screen = state.screen) {
    const copy = currentConfig().screenCopy?.[screen];
    if (!copy) return;
    const title = document.querySelector(`[data-screen="${screen}"] h1`);
    const description = document.querySelector(`[data-screen="${screen}"] .screen-description`);
    if (title) title.textContent = copy[0];
    if (description) description.textContent = copy[1];
  }

  function artworkStepFor(screen) {
    return {
      welcome: 'welcome',
      scope: 'scope',
      location: 'location',
      confirm: 'confirm',
      'update-check': 'check',
      'update-options': 'options',
      'update-confirm': 'confirm',
      'uninstall-options': 'options',
      'uninstall-confirm': 'confirm',
      progress: 'progress',
      finish: 'finish',
    }[screen] || 'welcome';
  }

  function syncArtwork(screen = state.screen) {
    if (!artwork) return;
    const step = artworkStepFor(screen);
    artwork.dataset.mode = state.mode;
    artwork.dataset.step = step;
    artwork.setAttribute('aria-label', `${currentConfig().label}流程：${step}`);
  }

  function applyRuntimeConfig(config) {
    if (!config || typeof config !== 'object') return;
    runtimeConfig = { ...runtimeConfig, ...config };
    modeConfig = createModeConfig(runtimeConfig.version, runtimeConfig.installedVersion);
    state.mode = ['install', 'update', 'uninstall'].includes(runtimeConfig.mode)
      ? runtimeConfig.mode
      : 'install';
    state.scope = runtimeConfig.scope === 'per-machine' ? 'per-machine' : 'per-user';
    state.path = String(runtimeConfig.targetPath || state.path);
    state.pathTouched = false;
    pathInput.value = state.path;

    setText('#window-version', versionLabel(runtimeConfig.version));
    setText('#installed-version', versionLabel(runtimeConfig.installedVersion, '已安装'));
    setText('#update-current-version', versionLabel(runtimeConfig.installedVersion, '已安装'));
    setText('#uninstall-version', versionLabel(runtimeConfig.installedVersion, '已安装'));
    setText('#target-version', versionLabel(runtimeConfig.version));
    setText('#confirm-app-version', `${versionLabel(runtimeConfig.version)} · Windows x64`);
    setText('#target-version-detail', 'Windows x64 · 本地安装包');
    setText('#update-target-version', versionLabel(runtimeConfig.version));
    setText('#update-target-detail', 'Windows x64 · 本地安装包');

    document.querySelectorAll('input[name="scope"]').forEach(input => {
      input.checked = input.value === state.scope;
    });
    document.querySelectorAll('.choice-card').forEach(card => {
      card.classList.toggle('is-selected', card.querySelector('input')?.checked === true);
    });
    syncWelcomeCopy();
    syncScreenCopy(state.screen);
    updateModeNote();
    syncArtwork();
    updateStepper(state.screen);
    updateActions(state.screen);
    syncSummary();
  }

  function applyForwardedRuntimeConfig(config) {
    if (!config || typeof config !== 'object') return;
    if (state.operationRunning) {
      showToast('当前操作正在进行，请等待完成后再处理新的安装请求。');
      return;
    }
    stopProgress();
    state.screen = 'welcome';
    state.progress = 0;
    state.operationError = null;
    state.operationHandled = false;
    operationError.hidden = true;
    operationErrorMessage.textContent = '';
    applyRuntimeConfig(config);
    setScreen('welcome');
    showToast(config.mode === 'update'
      ? '已切换到更新流程，正在继续处理新版本。'
      : config.mode === 'uninstall' ? '已切换到卸载流程。' : '已切换到安装流程。');
    autoStartConfiguredFlow();
  }

  function updateStepper(screen) {
    const config = currentConfig();
    const activeIndex = config.screenIndexes[screen] ?? 0;
    steps.forEach((step, index) => {
      const label = step.querySelector('.step-label');
      if (label) label.textContent = config.stepLabels[index] || '';
      step.classList.toggle('is-active', index === activeIndex);
      step.classList.toggle('is-done', index < activeIndex);
      if (index === activeIndex) step.setAttribute('aria-current', 'step');
      else step.removeAttribute('aria-current');
    });
  }

  function getActionCopy(screen) {
    if (state.operationError && screen === 'progress') {
      return { hint: '这次没搬利索，可以重试，或者回去改改清单。', secondary: '返回', primary: '再试一次', icon: true };
    }
    if (screen === 'welcome') {
      const welcomeAction = {
        install: { primary: '开始入住', secondary: '先不折腾了' },
        update: { primary: '开始换装', secondary: '先不换了' },
        uninstall: { primary: '送小猪出门', secondary: '再留一会儿' },
      }[state.mode];
      return { ...welcomeAction, hint: modeText[state.mode], icon: true };
    }
    if (screen === 'progress') {
      return { hint: currentConfig().progress.description, secondary: currentConfig().progress.cancel, primary: '', icon: false };
    }
    if (screen === 'finish') {
      const finish = currentConfig().finish;
      return { hint: finish.launch ? '小猪已经就位，随时可以开工。' : '小猪已经安全退租，后会有期。', secondary: '稍后关闭', primary: finish.launch && launchAfterFinish.checked ? '完成并叫醒' : '完成', icon: false };
    }
    return actionCopy[screen] || actionCopy.welcome;
  }

  function updateActions(screen) {
    const copy = getActionCopy(screen);
    actionHint.textContent = copy.hint;
    secondaryAction.textContent = copy.secondary;
    secondaryAction.disabled = false;
    primaryAction.textContent = copy.primary;
    primaryAction.disabled = state.operationRunning && !state.operationError;
    primaryAction.hidden = screen === 'progress' && !state.operationError;
    if (copy.icon && copy.primary) {
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.classList.add('button-icon');
      icon.innerHTML = '<use href="#icon-arrow"></use>';
      primaryAction.appendChild(icon);
    }
  }

  function updateModeNote() {
    const note = currentConfig().welcomeNote;
    const use = modeNote?.querySelector('use');
    const text = modeNote?.querySelector('span');
    if (use) use.setAttribute('href', `#${note.icon}`);
    if (text) text.textContent = note.text;
  }

  function syncSummary() {
    const selectedScope = state.scope === 'per-machine' ? '全家都能用' : '只给我用';
    if (summaryScope) summaryScope.textContent = selectedScope;
    if (summaryPath) summaryPath.textContent = state.path || '还没挑住址';
    if (summaryUpdateData) {
      summaryUpdateData.textContent = '设置和历史任务继续留着';
    }
    if (summaryUninstallData) {
      const keepData = document.querySelector('#preserve-uninstall-data')?.checked;
      const deleteCache = document.querySelector('#delete-uninstall-cache')?.checked;
      summaryUninstallData.textContent = keepData
        ? deleteCache ? '设置留下，缓存带走' : '设置和历史任务留下'
        : deleteCache ? '设置、历史任务和缓存都带走' : '设置和历史任务也带走';
    }
  }

  function syncProgressCopy() {
    const config = currentConfig().progress;
    progressTitle.textContent = config.title;
    progressDescription.textContent = config.description;
    phases.forEach((phase, index) => {
      phase.querySelector('[data-phase-label]').textContent = config.phases[index];
    });
  }

  function syncFinishCopy() {
    const finish = currentConfig().finish;
    finishTitle.textContent = finish.title;
    finishDescription.textContent = finish.description;
    finishSummaryText.textContent = finish.summary;
    finishVersion.textContent = finish.version;
    finishSummary.classList.toggle('is-uninstall', state.mode === 'uninstall');
    launchOption.hidden = !finish.launch;
    launchLabel.textContent = '现在就把小猪叫醒';
  }

  function setScreen(nextScreen) {
    if (!currentFlow().includes(nextScreen)) return;
    if (state.screen === 'progress' && nextScreen !== 'progress') stopProgress();
    state.screen = nextScreen;
    if (nextScreen !== 'progress') state.operationError = null;
    screens.forEach(screen => {
      const active = screen.dataset.screen === nextScreen;
      screen.classList.toggle('is-active', active);
      screen.hidden = !active;
    });
    syncScreenCopy(nextScreen);
    syncArtwork(nextScreen);
    updateStepper(nextScreen);
    updateActions(nextScreen);
    syncSummary();
    if (nextScreen === 'progress') {
      syncProgressCopy();
      resetProgress();
    }
    if (nextScreen === 'finish') syncFinishCopy();
  }

  function resetProgress() {
    state.progress = 0;
    operationError.hidden = true;
    operationErrorMessage.textContent = '';
    const firstEntry = currentConfig().progress.entries[0];
    updateProgress({
      percent: 0,
      phase: firstEntry.phase,
      stage: firstEntry.stage,
      file: firstEntry.file,
      count: typeof firstEntry.count === 'function' ? firstEntry.count(0) : firstEntry.count,
    });
  }

  function startDemoProgress() {
    stopProgress();
    state.progress = 0;
    updateProgress();
    state.timer = window.setInterval(() => {
      const increment = state.progress < 24 ? 4 : state.progress < 72 ? 3 : 2;
      state.progress = Math.min(100, state.progress + increment);
      updateProgress();
      if (state.progress >= 100) {
        stopProgress();
        state.completionTimer = window.setTimeout(() => {
          state.completionTimer = null;
          if (state.screen === 'progress') finishOperation({ success: true });
        }, 480);
      }
    }, 180);
  }

  function stopProgress() {
    if (state.timer) window.clearInterval(state.timer);
    if (state.completionTimer) window.clearTimeout(state.completionTimer);
    state.timer = null;
    state.completionTimer = null;
  }

  function updateProgress(progress = null) {
    const value = progress ? Math.min(100, Math.max(0, Number(progress.percent) || 0)) : state.progress;
    state.progress = value;
    const config = currentConfig().progress;
    const entry = config.entries.find(item => value < item.limit) || config.entries.at(-1);
    progressFill.style.transform = `scaleX(${value / 100})`;
    progressPercent.textContent = `${Math.round(value)}%`;
    progressTrack.setAttribute('aria-valuenow', String(Math.round(value)));
    progressStage.textContent = entry.stage;
    progressFile.textContent = progress?.file || entry.file;
    progressCount.textContent = progress?.count || (typeof entry.count === 'function' ? entry.count(value) : entry.count);
    setPhase(progress?.phase || entry.phase);
  }

  function setPhase(activePhase) {
    const order = ['prepare', 'write', 'shortcut', 'finish'];
    const activeIndex = order.indexOf(activePhase);
    phases.forEach(phase => {
      const index = order.indexOf(phase.dataset.phase);
      phase.classList.toggle('is-active', index === activeIndex);
      phase.classList.toggle('is-done', index < activeIndex);
    });
  }

  function operationPlan() {
    return {
      mode: state.mode,
      targetPath: state.path,
      scope: state.scope,
      desktopShortcut: state.mode === 'update'
        ? runtimeConfig.shortcuts?.desktop !== false
        : document.querySelector('#desktop-shortcut')?.checked !== false,
      startMenuShortcut: state.mode === 'update'
        ? runtimeConfig.shortcuts?.startMenu !== false
        : document.querySelector('#start-menu-shortcut')?.checked !== false,
      refreshShortcuts: state.mode !== 'update'
        || document.querySelector('#refresh-shortcuts')?.checked !== false,
      keepUserData: state.mode === 'update'
        ? true
        : document.querySelector('#preserve-uninstall-data')?.checked !== false,
      deleteCache: document.querySelector('#delete-uninstall-cache')?.checked === true,
    };
  }

  function handleOperationError(error) {
    const code = String(error?.code || 'INSTALLER_ERROR');
    if (code === 'CANCELLED') {
      const previousScreen = currentFlow()[currentFlow().indexOf('progress') - 1];
      state.operationRunning = false;
      state.operationHandled = true;
      setScreen(previousScreen);
      showToast(`${currentConfig().label}已取消，尚未完成文件替换。`);
      return;
    }
    state.operationRunning = false;
    state.operationHandled = true;
    state.operationError = error?.message || '操作没有完成，请重试。';
    operationErrorMessage.textContent = state.operationError;
    operationError.hidden = false;
    updateActions('progress');
  }

  function finishOperation(result) {
    if (state.operationHandled || state.screen !== 'progress') return;
    if (result?.delegated) {
      state.operationHandled = true;
      state.operationRunning = false;
      showToast('正在请求管理员权限，新的安装窗口即将打开。');
      return;
    }
    state.operationHandled = true;
    state.operationRunning = false;
    state.progress = 100;
    updateProgress({ percent: 100, phase: 'finish', stage: currentConfig().finish.title, file: '操作已完成。', count: '完成' });
    window.setTimeout(() => {
      if (state.screen === 'progress') setScreen('finish');
    }, 240);
  }

  async function beginOperation() {
    if (state.operationRunning) return;
    state.operationRunning = true;
    state.operationHandled = false;
    state.operationError = null;
    updateActions('progress');
    if (!runtime) {
      startDemoProgress();
      return;
    }
    try {
      const result = await runtime.start(operationPlan());
      finishOperation(result);
    } catch (error) {
      handleOperationError(error);
    }
  }

  function retryOperation() {
    state.operationError = null;
    state.operationHandled = false;
    operationError.hidden = true;
    setScreen('progress');
    void beginOperation();
  }

  async function finishAndClose() {
    const shouldLaunch = currentConfig().finish.launch && launchAfterFinish.checked;
    if (runtime) {
      if (shouldLaunch) {
        const result = await runtime.launch();
        if (!result?.success) showToast(result?.message || '应用启动失败，请从开始菜单手动打开。');
      }
      await runtime.close();
      return;
    }
    showToast(shouldLaunch ? '原型演示：将启动小猪wordTTS。' : '原型演示：安装器已关闭。');
  }

  function goForward() {
    if (state.screen === 'progress') {
      if (state.operationError) retryOperation();
      return;
    }
    if (state.screen === 'finish') {
      void finishAndClose();
      return;
    }
    if (state.screen === 'location' && !state.path.trim()) {
      showToast('请先填写安装位置。');
      pathInput.focus();
      return;
    }
    const flow = currentFlow();
    const nextIndex = flow.indexOf(state.screen) + 1;
    if (nextIndex <= 0 || nextIndex >= flow.length) return;
    const nextScreen = flow[nextIndex];
    setScreen(nextScreen);
    if (nextScreen === 'progress') void beginOperation();
  }

  function autoStartConfiguredFlow() {
    if (!runtimeConfig.autoStart || !runtimeConfig.fixedMode) return;
    const confirmScreen = state.mode === 'install'
      ? 'confirm'
      : state.mode === 'update' ? 'update-confirm' : 'uninstall-confirm';
    setScreen(confirmScreen);
    window.setTimeout(() => {
      if (state.screen === confirmScreen && !state.operationRunning) goForward();
    }, 420);
  }

  function goBack() {
    const flow = currentFlow();
    const previousIndex = flow.indexOf(state.screen) - 1;
    if (previousIndex >= 0) setScreen(flow[previousIndex]);
  }

  function handleSecondary() {
    if (state.screen === 'welcome') {
      if (runtime) void runtime.close();
      else showToast('原型演示：这里会退出安装器。');
    } else if (state.screen === 'progress') {
      if (state.operationError) {
        goBack();
        return;
      }
      if (state.operationRunning) {
        if (runtime) {
          void runtime.cancel();
          actionHint.textContent = '正在取消当前操作…';
          secondaryAction.disabled = true;
        } else {
          const previousScreen = currentFlow()[currentFlow().indexOf('progress') - 1];
          stopProgress();
          state.operationRunning = false;
          setScreen(previousScreen);
          showToast(`${currentConfig().label}已取消，尚未修改真实文件。`);
        }
      }
    } else if (state.screen === 'finish') {
      void finishAndClose();
    } else {
      goBack();
    }
  }

  document.querySelectorAll('input[name="scope"]').forEach(input => {
    input.addEventListener('change', () => {
      const previousScope = state.scope;
      const previousDefault = runtimeConfig.defaultTargetPaths?.[previousScope === 'per-machine' ? 'perMachine' : 'perUser'];
      state.scope = input.value;
      const nextDefault = runtimeConfig.defaultTargetPaths?.[state.scope === 'per-machine' ? 'perMachine' : 'perUser'];
      if (!state.pathTouched && nextDefault && (!previousDefault || state.path === previousDefault)) {
        state.path = nextDefault;
        pathInput.value = nextDefault;
      }
      document.querySelectorAll('.choice-card').forEach(card => {
        card.classList.toggle('is-selected', card.querySelector('input')?.checked === true);
      });
      syncSummary();
    });
  });

  pathInput.addEventListener('input', () => {
    state.pathTouched = true;
    state.path = pathInput.value;
    syncSummary();
  });

  document.querySelector('#browse-path')?.addEventListener('click', async () => {
    if (runtime) {
      const result = await runtime.chooseDirectory(state.path);
      if (!result?.canceled && result.path) {
        state.pathTouched = true;
        pathInput.value = result.path;
        state.path = result.path;
        syncSummary();
      }
      return;
    }
    const nextPath = state.scope === 'per-machine'
      ? 'C:\\Program Files\\小猪wordTTS'
      : 'D:\\Apps\\小猪wordTTS';
    state.pathTouched = true;
    pathInput.value = nextPath;
    state.path = nextPath;
    syncSummary();
    showToast('原型演示：实际版本这里会打开 Windows 文件夹选择器。');
  });

  ['#refresh-shortcuts', '#preserve-uninstall-data', '#delete-uninstall-cache'].forEach(selector => {
    document.querySelector(selector)?.addEventListener('change', syncSummary);
  });

  launchAfterFinish.addEventListener('change', () => {
    if (state.screen === 'finish') updateActions(state.screen);
  });

  primaryAction.addEventListener('click', goForward);
  secondaryAction.addEventListener('click', handleSecondary);

  document.querySelectorAll('[data-window-action]').forEach(button => {
    button.addEventListener('click', () => {
      const action = button.dataset.windowAction;
      if (runtime) {
        if (action === 'minimize') void runtime.minimize();
        else if (!state.operationRunning) void runtime.close();
        return;
      }
      showToast(action === 'minimize' ? '原型演示：窗口会最小化。' : '原型演示：窗口会关闭。');
    });
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') handleSecondary();
  });

  if (runtime) {
    runtime.onConfig(config => applyForwardedRuntimeConfig(config));
    runtime.onProgress(progress => {
      if (state.screen !== 'progress') return;
      updateProgress(progress);
    });
    runtime.onComplete(result => finishOperation(result));
    runtime.onError(error => handleOperationError(error));
  }

  async function boot() {
    if (runtime) {
      try {
        applyRuntimeConfig(await runtime.getConfig());
      } catch (error) {
        showToast(error.message || '无法读取安装程序配置。');
      }
    } else {
      let previewVersion = '';
      for (const source of ['./version.json', '../version.json', '../electron/package.json']) {
        try {
          const response = await fetch(source, { cache: 'no-store' });
          if (!response.ok) continue;
          const payload = await response.json();
          const candidate = payload?.version || payload;
          if (candidate) {
            previewVersion = String(candidate).trim();
            break;
          }
        } catch (_) {
          // A file:// preview may not permit fetch; the packaged installer
          // always receives appVersion through installerAPI instead.
        }
      }
      applyRuntimeConfig(
        ['install', 'update', 'uninstall'].includes(previewMode)
          ? { ...runtimeConfig, version: previewVersion || runtimeConfig.version, mode: previewMode, fixedMode: true, allowedModes: [previewMode] }
          : { ...runtimeConfig, version: previewVersion || runtimeConfig.version },
      );
    }
    syncWelcomeCopy();
    syncScreenCopy(state.screen);
    updateModeNote();
    syncArtwork();
    syncSummary();
    updateStepper(state.screen);
    updateActions(state.screen);
    autoStartConfiguredFlow();
    runtime?.ready?.();
  }

  void boot();
})();
