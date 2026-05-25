#!/usr/bin/env python3
"""Patch step5.html → step6.html: 计分逻辑 + 6种成就徽章 + 最高分记录 + 分数跳数动画"""


with open('/Users/gary/opprime/glink/projects/sandbox-builder/sandbox-builder-step5.html') as f:
    html = f.read()

# ===================================================================
# 1. NEW CSS — 插入到 </style> 之前
# ===================================================================
new_css = '''
    /* ================================================================
       === SCORE DISPLAY (分数面板)
       ================================================================ */
    #score-display {
      color: #f0c040;
      font-weight: 900;
      font-size: 16px;
      text-shadow: 0 0 10px rgba(240,192,64,0.5);
      min-width: 50px;
      text-align: center;
      transition: transform 0.08s;
    }
    #score-display.bounce {
      animation: scoreBounce 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    @keyframes scoreBounce {
      0% { transform: scale(1); }
      40% { transform: scale(1.35); color: #fff; }
      100% { transform: scale(1); }
    }

    #highscore-display {
      color: #ff9800;
      font-size: 12px;
      white-space: nowrap;
    }

    /* ================================================================
       === SCORE POP (分数弹出动画)
       ================================================================ */
    #score-pop-container {
      position: fixed; inset: 0; z-index: 50;
      pointer-events: none;
    }
    .score-pop {
      position: absolute;
      font-weight: 900;
      font-size: 24px;
      color: #f0c040;
      text-shadow: 0 0 10px rgba(240,192,64,0.8), 2px 2px 0 rgba(0,0,0,0.6);
      pointer-events: none;
      white-space: nowrap;
      animation: popFloat 1.2s ease-out forwards;
    }
    .score-pop.crit {
      font-size: 34px;
      color: #ff6d00;
      text-shadow: 0 0 16px rgba(255,109,0,0.9), 2px 2px 0 rgba(0,0,0,0.6);
    }
    .score-pop.combo {
      font-size: 28px;
      color: #e040fb;
      text-shadow: 0 0 12px rgba(224,64,251,0.8), 2px 2px 0 rgba(0,0,0,0.5);
    }
    @keyframes popFloat {
      0%   { opacity: 1; transform: translateY(0) scale(0.5); }
      15%  { opacity: 1; transform: translateY(-10px) scale(1.2); }
      40%  { opacity: 1; transform: translateY(-30px) scale(1); }
      100% { opacity: 0; transform: translateY(-80px) scale(0.8); }
    }

    /* ================================================================
       === ACHIEVEMENT TOAST (成就弹窗)
       ================================================================ */
    #achievement-container {
      position: fixed; top: 60px; right: 20px; z-index: 200;
      display: flex; flex-direction: column-reverse; gap: 8px;
      pointer-events: none;
    }
    .achievement-toast {
      display: flex; align-items: center; gap: 10px;
      background: linear-gradient(135deg, rgba(40,25,10,0.95) 0%, rgba(25,15,5,0.95) 100%);
      border: 2px solid #f0c040;
      border-radius: 12px;
      padding: 12px 16px;
      box-shadow: 0 0 24px rgba(240,192,64,0.4), inset 0 0 12px rgba(240,192,64,0.08);
      animation: achievementSlide 0.5s cubic-bezier(0.34, 1.56, 0.64, 1), 
                 achievementFadeOut 0.5s 4s ease-in forwards;
      max-width: 280px;
    }
    .achievement-toast .ach-icon {
      font-size: 36px;
      flex-shrink: 0;
      animation: achIconPulse 0.6s ease-in-out 3;
    }
    .achievement-toast .ach-info {
      display: flex; flex-direction: column; gap: 3px;
    }
    .achievement-toast .ach-title {
      color: #f0c040; font-weight: 900; font-size: 14px;
    }
    .achievement-toast .ach-desc {
      color: #bbb; font-size: 11px;
    }
    @keyframes achievementSlide {
      from { transform: translateX(120%); opacity: 0; }
      to   { transform: translateX(0); opacity: 1; }
    }
    @keyframes achievementFadeOut {
      to { opacity: 0; transform: translateX(30px); }
    }
    @keyframes achIconPulse {
      0%, 100% { transform: scale(1); }
      50% { transform: scale(1.25); }
    }

    /* ================================================================
       === ACHIEVEMENT BADGE GRID (侧面板成就展示)
       ================================================================ */
    .badge-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 6px;
    }
    .badge {
      width: 44px; height: 44px;
      background: rgba(255,255,255,0.04);
      border: 2px solid rgba(255,255,255,0.08);
      border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      font-size: 22px;
      position: relative;
      transition: all 0.3s;
      opacity: 0.3;
      filter: grayscale(1);
    }
    .badge.unlocked {
      opacity: 1;
      filter: grayscale(0);
      border-color: rgba(240,192,64,0.5);
      box-shadow: 0 0 10px rgba(240,192,64,0.2);
    }
    .badge .badge-tooltip {
      display: none;
      position: absolute; bottom: 110%;
      background: rgba(0,0,0,0.9);
      border: 1px solid rgba(240,192,64,0.4);
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 10px; color: #ddd;
      white-space: nowrap;
      z-index: 20;
    }
    .badge:hover .badge-tooltip { display: block; }

    /* ================================================================
       === COMBO INDICATOR
       ================================================================ */
    #combo-indicator {
      position: fixed; top: 50%; left: 50%; z-index: 60;
      transform: translate(-50%, 0);
      font-size: 16px; font-weight: 900;
      color: #e040fb;
      text-shadow: 0 0 12px rgba(224,64,251,0.5);
      pointer-events: none;
      opacity: 0;
      transition: opacity 0.15s, transform 0.1s;
    }
    #combo-indicator.active { opacity: 1; transform: translate(-50%, -60px); }

'''

html = html.replace('  </style>', new_css + '\n  </style>')

# ===================================================================
# 2. NEW HTML — 修改 top-bar + 添加新容器
# ===================================================================

# 在 top-bar logo 后插入分数显示
old_topbar_start = '''  <div id="top-bar">
    <div class="logo pixel-text">
      <span class="cube-icon"></span>沙盒建造
    </div>
    <div class="stat">
      🧱 <span class="val" id="block-count">0</span>
    </div>'''

new_topbar_start = '''  <div id="top-bar">
    <div class="logo pixel-text">
      <span class="cube-icon"></span>沙盒建造
    </div>
    <div class="stat" style="background:rgba(240,192,64,0.1);border-color:rgba(240,192,64,0.2);">
      🏆 <span class="val" id="score-display">0</span>
      <span id="highscore-display" style="font-size:10px;color:#ff9800;">⭐ 0</span>
    </div>
    <div class="stat">
      🧱 <span class="val" id="block-count">0</span>
    </div>'''

html = html.replace(old_topbar_start, new_topbar_start)

# 在 </body> 前添加新容器
new_html_before_body = '''
  <!-- =============================================================
       === SCORE POP CONTAINER
       ============================================================= -->
  <div id="score-pop-container"></div>

  <!-- =============================================================
       === ACHIEVEMENT CONTAINER
       ============================================================= -->
  <div id="achievement-container"></div>

  <!-- =============================================================
       === COMBO INDICATOR
       ============================================================= -->
  <div id="combo-indicator"></div>
'''
html = html.replace('</body>', new_html_before_body + '\n</body>')

# ===================================================================
# 3. NEW JS — 在 CONSTANTS 之后插入游戏状态和成就定义
# ===================================================================

new_state_code = '''
    // ============================================================
    // === GAME STATE (计分/成就系统)
    // ============================================================
    const gameState = {
      score: 0,
      highScore: parseInt(localStorage.getItem('sandbox-highscore') || '0'),
      combo: 0,
      comboTimer: null,
      comboTimeout: null,
      placedCount: 0,
      removedCount: 0,
      placedHighCount: 0,   // 在 y>=15 高度放置的方块数
      typesUsed: new Set(),
      rapidPlaced: [],       // 时间戳数组，用于检测闪电手
      unlocked: JSON.parse(localStorage.getItem('sandbox-achievements') || '[]'),
    };

    // ============================================================
    // === ACHIEVEMENT DEFINITIONS (6种成就徽章)
    // ============================================================
    const ACHIEVEMENTS = [
      {
        id: 'builder',
        icon: '🧱',
        name: '建筑师',
        desc: '累计放置 100 个方块',
        check: () => gameState.placedCount >= 100,
      },
      {
        id: 'destroyer',
        icon: '💥',
        name: '拆迁队',
        desc: '累计拆除 50 个方块',
        check: () => gameState.removedCount >= 50,
      },
      {
        id: 'climber',
        icon: '🏔️',
        name: '登高者',
        desc: '在高度 15+ 放置 10 个方块',
        check: () => gameState.placedHighCount >= 10,
      },
      {
        id: 'collector',
        icon: '🌈',
        name: '收藏家',
        desc: '使用过全部 6 种方块',
        check: () => gameState.typesUsed.size >= 6,
      },
      {
        id: 'speedster',
        icon: '⚡',
        name: '闪电手',
        desc: '30 秒内放置 20 个方块',
        check: () => {
          const now = Date.now();
          gameState.rapidPlaced = gameState.rapidPlaced.filter(t => now - t < 30000);
          return gameState.rapidPlaced.length >= 20;
        },
      },
      {
        id: 'master',
        icon: '👑',
        name: '全能王',
        desc: '解锁全部其他成就',
        check: () => {
          const otherIds = ACHIEVEMENTS.filter(a => a.id !== 'master').map(a => a.id);
          return otherIds.every(id => gameState.unlocked.includes(id));
        },
      },
    ];

    // ============================================================
    // === SCORE FUNCTIONS
    // ============================================================
    function addScore(points, worldPos, bonusType) {
      gameState.score += points;
      
      // Combo system
      gameState.combo++;
      if (gameState.comboTimeout) clearTimeout(gameState.comboTimeout);
      gameState.comboTimeout = setTimeout(() => {
        gameState.combo = 0;
        updateComboIndicator();
      }, 2500);

      // Combo bonus (every 5 combo)
      if (gameState.combo > 0 && gameState.combo % 5 === 0) {
        const comboBonus = gameState.combo * 2;
        gameState.score += comboBonus;
        spawnScorePop(comboBonus, worldPos, 'combo');
        showToast(`🔥 ${gameState.combo}连击! +${comboBonus}`, 'warning');
      } else {
        spawnScorePop(points, worldPos, bonusType || '');
      }

      // Update high score
      if (gameState.score > gameState.highScore) {
        gameState.highScore = gameState.score;
        localStorage.setItem('sandbox-highscore', gameState.highScore);
      }

      updateScoreDisplay();
      updateComboIndicator();
      checkAchievements();
    }

    function spawnScorePop(points, worldPos, cssClass) {
      if (!worldPos) return;
      
      // Project 3D world position to 2D screen position
      const vector = worldPos.clone().project(camera);
      const x = (vector.x * 0.5 + 0.5) * window.innerWidth;
      const y = (-vector.y * 0.5 + 0.5) * window.innerHeight;

      const pop = document.createElement('div');
      pop.className = 'score-pop' + (cssClass ? ' ' + cssClass : '');
      pop.textContent = '+' + points;
      pop.style.left = x + 'px';
      pop.style.top = y + 'px';
      
      document.getElementById('score-pop-container').appendChild(pop);
      
      // Auto remove after animation
      setTimeout(() => pop.remove(), 1300);
    }

    function updateScoreDisplay() {
      const el = document.getElementById('score-display');
      el.textContent = gameState.score;
      el.classList.remove('bounce');
      void el.offsetWidth; // reflow
      el.classList.add('bounce');

      const hsEl = document.getElementById('highscore-display');
      hsEl.textContent = '⭐ ' + gameState.highScore;
    }

    function updateComboIndicator() {
      const el = document.getElementById('combo-indicator');
      if (gameState.combo >= 3) {
        el.textContent = '🔥 ' + gameState.combo + ' 连击!';
        el.classList.add('active');
      } else {
        el.classList.remove('active');
      }
    }

    // ============================================================
    // === ACHIEVEMENT FUNCTIONS
    // ============================================================
    function checkAchievements() {
      for (const ach of ACHIEVEMENTS) {
        if (gameState.unlocked.includes(ach.id)) continue;
        if (ach.check()) {
          unlockAchievement(ach);
        }
      }
    }

    function unlockAchievement(ach) {
      gameState.unlocked.push(ach.id);
      localStorage.setItem('sandbox-achievements', JSON.stringify(gameState.unlocked));

      // Show achievement toast
      const toast = document.createElement('div');
      toast.className = 'achievement-toast';
      toast.innerHTML = `
        <span class="ach-icon">${ach.icon}</span>
        <div class="ach-info">
          <span class="ach-title">🏅 成就解锁!</span>
          <span class="ach-desc">${ach.icon} ${ach.name} — ${ach.desc}</span>
        </div>
      `;
      document.getElementById('achievement-container').appendChild(toast);
      setTimeout(() => toast.remove(), 5000);

      // Update badge display
      updateBadgeDisplay();
      
      showToast(`🏅 成就解锁: ${ach.name}!`, 'success');
    }

    function updateBadgeDisplay() {
      const badges = document.querySelectorAll('.badge');
      badges.forEach(badge => {
        const achId = badge.dataset.achId;
        if (gameState.unlocked.includes(achId)) {
          badge.classList.add('unlocked');
        }
      });
    }

    function buildBadgeGrid() {
      const panel = document.querySelector('#side-panel .panel-content');
      const h3 = document.createElement('h3');
      h3.textContent = '🏅 成就徽章';
      panel.appendChild(h3);

      const grid = document.createElement('div');
      grid.className = 'badge-grid';
      for (const ach of ACHIEVEMENTS) {
        const badge = document.createElement('div');
        badge.className = 'badge' + (gameState.unlocked.includes(ach.id) ? ' unlocked' : '');
        badge.dataset.achId = ach.id;
        badge.textContent = ach.icon;
        badge.title = ach.name;
        const tooltip = document.createElement('span');
        tooltip.className = 'badge-tooltip';
        tooltip.textContent = ach.name + ': ' + ach.desc;
        badge.appendChild(tooltip);
        grid.appendChild(badge);
      }
      panel.appendChild(grid);
    }

    // ============================================================
    // === RESET SCORE (清空时调用)
    // ============================================================
    function resetScore() {
      gameState.score = 0;
      gameState.combo = 0;
      updateScoreDisplay();
      updateComboIndicator();
    }

'''

# Insert after "const BLOCK_TYPE_KEYS = Object.keys(BLOCK_TYPES);"
insert_marker = "const BLOCK_TYPE_KEYS = Object.keys(BLOCK_TYPES);"
html = html.replace(insert_marker, insert_marker + '\n' + new_state_code)

# ===================================================================
# 4. PATCH placeBlock — 添加计分
# ===================================================================
old_placeBlock = '''      createPhysicsBodyForBlock(mesh, position, 1);
      blocks.push({ mesh, type });
      undoStack.push({ mesh, type });
      updateBlockCount();
      updatePreviewPosition();
      return true;'''

new_placeBlock = '''      createPhysicsBodyForBlock(mesh, position, 1);
      blocks.push({ mesh, type });
      undoStack.push({ mesh, type });

      // Scoring
      let points = 10; // base
      if (position.y >= 15) { points += 10; gameState.placedHighCount++; }
      else if (position.y >= 8) { points += 5; }
      gameState.placedCount++;
      gameState.typesUsed.add(type);
      gameState.rapidPlaced.push(Date.now());
      addScore(points, position.clone());

      updateBlockCount();
      updatePreviewPosition();
      return true;'''

html = html.replace(old_placeBlock, new_placeBlock)

# ===================================================================
# 5. PATCH removeBlock — 添加计分
# ===================================================================
old_removeBlock = '''      scene.remove(block.mesh);
      removePhysicsBodyForBlock(block.mesh);
      // Remove from undo stack if present
      const undoIdx = undoStack.findIndex(u => u.mesh === mesh);
      if (undoIdx !== -1) undoStack.splice(undoIdx, 1);
      blocks.splice(idx, 1);
      updateBlockCount();
      return true;'''

new_removeBlock = '''      scene.remove(block.mesh);
      removePhysicsBodyForBlock(block.mesh);
      // Remove from undo stack if present
      const undoIdx = undoStack.findIndex(u => u.mesh === mesh);
      if (undoIdx !== -1) undoStack.splice(undoIdx, 1);
      blocks.splice(idx, 1);

      // Scoring
      gameState.removedCount++;
      addScore(5, block.mesh.position.clone());

      updateBlockCount();
      return true;'''

html = html.replace(old_removeBlock, new_removeBlock)

# ===================================================================
# 6. PATCH undoLastBlock — 撤销扣分
# ===================================================================
old_undo = '''      scene.remove(last.mesh);
      removePhysicsBodyForBlock(last.mesh);
      blocks.splice(idx, 1);
      updateBlockCount();
      return true;'''

new_undo = '''      scene.remove(last.mesh);
      removePhysicsBodyForBlock(last.mesh);
      blocks.splice(idx, 1);

      // Undo scoring: deduct the points earned
      gameState.score = Math.max(0, gameState.score - 10);
      gameState.placedCount = Math.max(0, gameState.placedCount - 1);
      updateScoreDisplay();
      
      updateBlockCount();
      return true;'''

html = html.replace(old_undo, new_undo)

# ===================================================================
# 7. PATCH clearAllBlocks — 重置分数
# ===================================================================
old_clear = '''      undoStack.length = 0;
      currentIntersection = null;
      previewMesh.visible = false;
      updateBlockCount();'''

new_clear = '''      undoStack.length = 0;
      currentIntersection = null;
      previewMesh.visible = false;
      resetScore();
      updateBlockCount();'''

html = html.replace(old_clear, new_clear)

# ===================================================================
# 8. PATCH animation loop — 添加 combo decay
# ===================================================================
old_animate = '''    function animate() {
      requestAnimationFrame(animate);

      const delta = Math.min(clock.getDelta(), 0.1);

      // Physics step
      world.step(1 / 60, delta, 2);'''

new_animate = '''    function animate() {
      requestAnimationFrame(animate);

      const delta = Math.min(clock.getDelta(), 0.1);

      // Combo decay timer
      if (gameState.comboTimeout === null && gameState.combo > 0) {
        gameState.combo = 0;
        updateComboIndicator();
      }

      // Physics step
      world.step(1 / 60, delta, 2);'''

html = html.replace(old_animate, new_animate)

# ===================================================================
# 9. PATCH INIT UI — 初始化新UI组件
# ===================================================================
old_init = '''    buildHotbar();
    updatePanelCurrentType();
    updateBlockCount();'''

new_init = '''    buildHotbar();
    buildBadgeGrid();
    updatePanelCurrentType();
    updateBlockCount();
    updateScoreDisplay();'''

html = html.replace(old_init, new_init)

# ===================================================================
# 10. PATCH BOOT LOG — 添加新功能日志
# ===================================================================
old_boot = '''    console.log('🏗️  沙盒建造游戏 Step5 · UI系统 初始化完成');'''

new_boot = '''    console.log('🏗️  沙盒建造游戏 Step6 · 计分+成就系统 初始化完成');
    console.log('   🏆 计分系统 (放置+10/拆除+5)');
    console.log('   🔥 连击加成 (每5连击额外加分)');
    console.log('   ⭐ 最高分记录 (localStorage)');
    console.log('   🏅 6种成就徽章');
    console.log('   💫 分数跳数动画');
    console.log('   📊 已解锁成就:', gameState.unlocked.length, '/', ACHIEVEMENTS.length);'''

html = html.replace(old_boot, new_boot)

# ===================================================================
# WRITE OUTPUT
# ===================================================================
output_path = '/Users/gary/opprime/glink/projects/sandbox-builder/sandbox-builder-step6.html'
with open(output_path, 'w') as f:
    f.write(html)

print(f'✅ step6.html written ({len(html)} bytes)')
