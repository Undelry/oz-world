// ============================
// OZ Demo Script — VTuber-style automated walkthrough
// Usage: load in oz_world.html via ?demo=1 URL parameter
// ============================

class OZDemoController {
  constructor() {
    this.running = false;
    this.startTime = 0;
    this.timeline = [];
    this.currentStep = 0;
    this.avatarEl = null;
    this.subtitleEl = null;
  }

  // Build the demo timeline
  buildTimeline(deps) {
    // deps = { voiceNavigator, showDialog, hideDialog, workers, WORKER_TYPES,
    //          camera, controls, scene, showNotification, ozSpeak,
    //          switchToFirstPerson, switchToOrbital, THREE }

    const { voiceNavigator, showDialog, hideDialog, workers, WORKER_TYPES,
            camera, controls, showNotification, ozSpeak,
            switchToFirstPerson, switchToOrbital, THREE } = deps;

    const findWorker = (type) => workers.find(w => w.type === type);
    const coderWorker = findWorker('coder');
    const reviewerWorker = findWorker('reviewer');
    const researcherWorker = findWorker('researcher');

    this.timeline = [
      // === 0s: Opening ===
      {
        time: 0,
        action: () => {
          this.setAvatar('smile');
          this.showSubtitle('');
        }
      },
      {
        time: 1.0,
        action: () => {
          this.setAvatar('talk');
          this.showSubtitle('こんにちは！OZの世界へようこそ！');
          ozSpeak('こんにちは！OZの世界へようこそ！');
        }
      },
      {
        time: 3.5,
        action: () => {
          this.setAvatar('smile');
          this.showSubtitle('');
        }
      },

      // === 4s: Camera pan — slow orbit ===
      {
        time: 4.0,
        action: () => {
          // Smooth camera pan to show the world
          const hitomiPos = new THREE.Vector3(0, 8, -2);
          voiceNavigator.tweenDuration = 3.0;
          voiceNavigator.flyTo(hitomiPos);
        }
      },

      // === 6s: Explain OZ ===
      {
        time: 6.5,
        action: () => {
          this.setAvatar('talk');
          this.showSubtitle('OZは音声で操作できる3Dバーチャルワールドです');
          ozSpeak('OZは音声で操作できる3Dバーチャルワールドです');
        }
      },
      {
        time: 9.5,
        action: () => {
          this.setAvatar('smile');
          this.showSubtitle('');
        }
      },

      // === 10s: Navigate to Coder ===
      {
        time: 10.0,
        action: () => {
          this.setAvatar('think');
          // Show simulated voice command
          this.showVoiceCommand('coderのところに行って');
          showNotification('Voice: "coderのところに行って"');
          voiceNavigator.tweenDuration = 2.0;
          if (coderWorker) {
            voiceNavigator.flyTo(coderWorker.group.position.clone());
          }
        }
      },
      {
        time: 12.5,
        action: () => {
          this.hideVoiceCommand();
        }
      },

      // === 14s: Open dialog with Coder ===
      {
        time: 14.0,
        action: () => {
          this.setAvatar('surprise');
          if (coderWorker) {
            showDialog(coderWorker);
          }
        }
      },
      {
        time: 16.5,
        action: () => {
          this.setAvatar('smile');
        }
      },

      // === 18s: Explain interaction ===
      {
        time: 18.0,
        action: () => {
          hideDialog();
          this.setAvatar('talk');
          this.showSubtitle('ワーカーたちに話しかけると声で返事してくれます');
          ozSpeak('ワーカーたちに話しかけると声で返事してくれます');
        }
      },
      {
        time: 21.0,
        action: () => {
          this.setAvatar('smile');
          this.showSubtitle('');
        }
      },

      // === 22s: Navigate to Monitor ===
      {
        time: 22.0,
        action: () => {
          this.setAvatar('think');
          this.showVoiceCommand('モニターを見せて');
          showNotification('Voice: "モニターを見せて"');
          const monitorPos = new THREE.Vector3(0, 12, -8);
          voiceNavigator.tweenDuration = 1.8;
          voiceNavigator.flyTo(monitorPos);
        }
      },
      {
        time: 24.0,
        action: () => {
          this.hideVoiceCommand();
          this.setAvatar('smile');
        }
      },

      // === 26s: Navigate to purple island ===
      {
        time: 26.0,
        action: () => {
          this.showVoiceCommand('紫の島に行って');
          showNotification('Voice: "紫の島に行って"');
          this.setAvatar('surprise');
          // Purple island is subIslands[4] — position approx (0, 10, -32)
          const purplePos = new THREE.Vector3(0, 10, -32);
          voiceNavigator.tweenDuration = 2.0;
          voiceNavigator.flyTo(purplePos);
        }
      },
      {
        time: 28.5,
        action: () => {
          this.hideVoiceCommand();
          this.setAvatar('smile');
        }
      },

      // === 29s: Economy reveal — fly camera high to show coins flying ===
      {
        time: 29.0,
        action: () => {
          this.setAvatar('surprise');
          this.showSubtitle('そしてエージェント同士は、暗号通貨で取引しています');
          ozSpeak('そしてエージェント同士は、暗号通貨で取引しています', 'Kyoko', 200, 'hitomi');
          // Fly camera high above the main island so coin animations are visible
          voiceNavigator.tweenDuration = 2.5;
          voiceNavigator.flyTo(new THREE.Vector3(0, 8, 8));
        }
      },
      {
        time: 33.0,
        action: () => {
          this.setAvatar('talk');
          this.showSubtitle('全てのタスク・LLM呼び出し・通知が OZコインで精算される');
          ozSpeak('全てのタスク、LLM呼び出し、通知がOZコインで精算されます', 'Kyoko', 200, 'hitomi');
        }
      },
      {
        time: 36.5,
        action: () => {
          this.setAvatar('smile');
          this.showSubtitle('');
        }
      },

      // === 38s: Final message ===
      {
        time: 38.0,
        action: () => {
          this.setAvatar('talk');
          this.showSubtitle('OZ — AIエージェントが自律的に経済を回す世界');
          ozSpeak('OZ。AIエージェントが、自律的に経済を回す世界', 'Kyoko', 200, 'hitomi');
        }
      },
      {
        time: 41.0,
        action: () => {
          this.setAvatar('smile');
          this.showSubtitle('');
        }
      },

      // === Hold final state, fly back to overview ===
      {
        time: 42.0,
        action: () => {
          this.setAvatar('smile');
          this.showSubtitle('OZ — AI Economy');
          voiceNavigator.tweenDuration = 3.0;
          voiceNavigator.flyTo(new THREE.Vector3(0, 6, 4));
        }
      },
      {
        time: 48.0,
        action: () => {
          this.running = false;
          this.showSubtitle('');
        }
      },
    ];
  }

  start(deps) {
    this.buildTimeline(deps);
    this.running = true;
    this.startTime = performance.now();
    this.currentStep = 0;
    console.log('[OZ Demo] Started — timeline has', this.timeline.length, 'steps');
  }

  // Called every frame from animate()
  update() {
    if (!this.running) return;

    const elapsed = (performance.now() - this.startTime) / 1000;

    while (this.currentStep < this.timeline.length) {
      const step = this.timeline[this.currentStep];
      if (elapsed >= step.time) {
        console.log(`[OZ Demo] Step ${this.currentStep} at ${step.time}s`);
        step.action();
        this.currentStep++;
      } else {
        break;
      }
    }
  }

  // === Avatar expression control ===
  setAvatar(expression) {
    const avatar = document.getElementById('vtuber-avatar');
    if (!avatar) return;

    const canvas = avatar.querySelector('canvas');
    if (!canvas) return;

    this._drawAvatar(canvas, expression);
    // Set lip-sync class
    avatar.classList.remove('talking');
    if (expression === 'talk') {
      avatar.classList.add('talking');
    }
  }

  _drawAvatar(canvas, expression) {
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;

    ctx.clearRect(0, 0, w, h);

    // --- Hair (back layer) ---
    ctx.fillStyle = '#3a2c5e';
    ctx.beginPath();
    ctx.ellipse(w/2, h*0.32, w*0.42, h*0.38, 0, 0, Math.PI * 2);
    ctx.fill();

    // --- Face ---
    ctx.fillStyle = '#fce4d6';
    ctx.beginPath();
    ctx.ellipse(w/2, h*0.42, w*0.28, h*0.30, 0, 0, Math.PI * 2);
    ctx.fill();

    // --- Hair bangs (front) ---
    ctx.fillStyle = '#3a2c5e';
    ctx.beginPath();
    ctx.moveTo(w*0.22, h*0.25);
    ctx.quadraticCurveTo(w*0.3, h*0.08, w*0.5, h*0.12);
    ctx.quadraticCurveTo(w*0.7, h*0.08, w*0.78, h*0.25);
    ctx.quadraticCurveTo(w*0.72, h*0.32, w*0.65, h*0.35);
    ctx.lineTo(w*0.58, h*0.28);
    ctx.lineTo(w*0.50, h*0.33);
    ctx.lineTo(w*0.42, h*0.28);
    ctx.lineTo(w*0.35, h*0.35);
    ctx.quadraticCurveTo(w*0.28, h*0.32, w*0.22, h*0.25);
    ctx.fill();

    // Side hair strands
    ctx.beginPath();
    ctx.moveTo(w*0.20, h*0.30);
    ctx.quadraticCurveTo(w*0.12, h*0.50, w*0.16, h*0.68);
    ctx.lineTo(w*0.22, h*0.65);
    ctx.quadraticCurveTo(w*0.18, h*0.48, w*0.25, h*0.33);
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(w*0.80, h*0.30);
    ctx.quadraticCurveTo(w*0.88, h*0.50, w*0.84, h*0.68);
    ctx.lineTo(w*0.78, h*0.65);
    ctx.quadraticCurveTo(w*0.82, h*0.48, w*0.75, h*0.33);
    ctx.fill();

    // --- Eyes ---
    const eyeY = h * 0.40;
    const leftEyeX = w * 0.38;
    const rightEyeX = w * 0.62;
    const eyeW = w * 0.08;
    const eyeH = h * 0.07;

    if (expression === 'smile') {
      // Happy closed eyes (upward arcs)
      ctx.strokeStyle = '#333';
      ctx.lineWidth = 2.5;
      ctx.lineCap = 'round';
      [leftEyeX, rightEyeX].forEach(ex => {
        ctx.beginPath();
        ctx.arc(ex, eyeY, eyeW*0.7, Math.PI * 1.1, Math.PI * 1.9);
        ctx.stroke();
      });
    } else if (expression === 'surprise') {
      // Wide open eyes
      [leftEyeX, rightEyeX].forEach(ex => {
        // White
        ctx.fillStyle = '#fff';
        ctx.beginPath();
        ctx.ellipse(ex, eyeY, eyeW*1.1, eyeH*1.2, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#333';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        // Iris
        ctx.fillStyle = '#6b4fa0';
        ctx.beginPath();
        ctx.ellipse(ex, eyeY, eyeW*0.6, eyeH*0.7, 0, 0, Math.PI * 2);
        ctx.fill();
        // Pupil
        ctx.fillStyle = '#222';
        ctx.beginPath();
        ctx.ellipse(ex, eyeY, eyeW*0.3, eyeH*0.35, 0, 0, Math.PI * 2);
        ctx.fill();
        // Highlight
        ctx.fillStyle = '#fff';
        ctx.beginPath();
        ctx.ellipse(ex - eyeW*0.2, eyeY - eyeH*0.2, eyeW*0.15, eyeH*0.15, 0, 0, Math.PI * 2);
        ctx.fill();
      });
    } else {
      // Normal / talk / think — open eyes with iris
      [leftEyeX, rightEyeX].forEach(ex => {
        // White
        ctx.fillStyle = '#fff';
        ctx.beginPath();
        ctx.ellipse(ex, eyeY, eyeW, eyeH, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#333';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        // Iris
        ctx.fillStyle = '#6b4fa0';
        ctx.beginPath();
        ctx.ellipse(ex, eyeY + eyeH*0.05, eyeW*0.55, eyeH*0.6, 0, 0, Math.PI * 2);
        ctx.fill();
        // Pupil
        ctx.fillStyle = '#222';
        ctx.beginPath();
        ctx.ellipse(ex, eyeY + eyeH*0.05, eyeW*0.25, eyeH*0.3, 0, 0, Math.PI * 2);
        ctx.fill();
        // Highlight
        ctx.fillStyle = '#fff';
        ctx.beginPath();
        ctx.ellipse(ex - eyeW*0.2, eyeY - eyeH*0.15, eyeW*0.12, eyeH*0.12, 0, 0, Math.PI * 2);
        ctx.fill();
      });
    }

    // Eyebrows
    ctx.strokeStyle = '#3a2c5e';
    ctx.lineWidth = 2;
    ctx.lineCap = 'round';
    if (expression === 'surprise') {
      // Raised eyebrows
      [leftEyeX, rightEyeX].forEach(ex => {
        ctx.beginPath();
        ctx.arc(ex, eyeY - eyeH*2.2, eyeW*1.0, Math.PI*1.15, Math.PI*1.85);
        ctx.stroke();
      });
    } else if (expression === 'think') {
      // Tilted eyebrows
      ctx.beginPath();
      ctx.moveTo(leftEyeX - eyeW, eyeY - eyeH*1.5);
      ctx.lineTo(leftEyeX + eyeW, eyeY - eyeH*1.8);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(rightEyeX - eyeW, eyeY - eyeH*1.8);
      ctx.lineTo(rightEyeX + eyeW, eyeY - eyeH*1.5);
      ctx.stroke();
    } else {
      [leftEyeX, rightEyeX].forEach(ex => {
        ctx.beginPath();
        ctx.arc(ex, eyeY - eyeH*1.6, eyeW*0.9, Math.PI*1.2, Math.PI*1.8);
        ctx.stroke();
      });
    }

    // --- Blush ---
    if (expression === 'smile' || expression === 'surprise') {
      ctx.fillStyle = 'rgba(255, 130, 130, 0.25)';
      ctx.beginPath();
      ctx.ellipse(leftEyeX - eyeW*0.3, eyeY + eyeH*1.5, eyeW*0.8, eyeH*0.4, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.ellipse(rightEyeX + eyeW*0.3, eyeY + eyeH*1.5, eyeW*0.8, eyeH*0.4, 0, 0, Math.PI * 2);
      ctx.fill();
    }

    // --- Nose ---
    ctx.fillStyle = '#e8c4b0';
    ctx.beginPath();
    ctx.moveTo(w*0.50, h*0.46);
    ctx.lineTo(w*0.48, h*0.50);
    ctx.lineTo(w*0.52, h*0.50);
    ctx.fill();

    // --- Mouth ---
    const mouthY = h * 0.56;
    if (expression === 'talk') {
      // Open mouth (will be animated by CSS)
      ctx.fillStyle = '#c44';
      ctx.beginPath();
      ctx.ellipse(w/2, mouthY, w*0.05, h*0.035, 0, 0, Math.PI * 2);
      ctx.fill();
      // Teeth hint
      ctx.fillStyle = '#fff';
      ctx.beginPath();
      ctx.ellipse(w/2, mouthY - h*0.01, w*0.04, h*0.012, 0, 0, Math.PI);
      ctx.fill();
    } else if (expression === 'smile') {
      // Smile
      ctx.strokeStyle = '#c44';
      ctx.lineWidth = 2;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.arc(w/2, mouthY - h*0.02, w*0.06, Math.PI*0.15, Math.PI*0.85);
      ctx.stroke();
    } else if (expression === 'surprise') {
      // O mouth
      ctx.fillStyle = '#c44';
      ctx.beginPath();
      ctx.ellipse(w/2, mouthY, w*0.04, h*0.04, 0, 0, Math.PI * 2);
      ctx.fill();
    } else if (expression === 'think') {
      // Wavy line
      ctx.strokeStyle = '#c44';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(w*0.44, mouthY);
      ctx.quadraticCurveTo(w*0.48, mouthY - h*0.015, w*0.52, mouthY);
      ctx.quadraticCurveTo(w*0.56, mouthY + h*0.015, w*0.58, mouthY);
      ctx.stroke();
    } else {
      // Neutral
      ctx.strokeStyle = '#c44';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(w*0.45, mouthY);
      ctx.lineTo(w*0.55, mouthY);
      ctx.stroke();
    }

    // --- Neck ---
    ctx.fillStyle = '#fce4d6';
    ctx.fillRect(w*0.44, h*0.68, w*0.12, h*0.06);

    // --- Shoulders / clothes ---
    ctx.fillStyle = '#8b5cf6';
    ctx.beginPath();
    ctx.moveTo(w*0.18, h*0.98);
    ctx.quadraticCurveTo(w*0.25, h*0.74, w*0.44, h*0.73);
    ctx.lineTo(w*0.56, h*0.73);
    ctx.quadraticCurveTo(w*0.75, h*0.74, w*0.82, h*0.98);
    ctx.lineTo(w*0.18, h*0.98);
    ctx.fill();

    // Collar detail
    ctx.fillStyle = '#fff';
    ctx.beginPath();
    ctx.moveTo(w*0.44, h*0.73);
    ctx.lineTo(w*0.50, h*0.82);
    ctx.lineTo(w*0.56, h*0.73);
    ctx.fill();

    // --- Hair accessory (star clip) ---
    ctx.fillStyle = '#ffd166';
    ctx.save();
    ctx.translate(w*0.73, h*0.28);
    const starR = w*0.04;
    ctx.beginPath();
    for (let i = 0; i < 5; i++) {
      const angle = (i * 4 * Math.PI / 5) - Math.PI/2;
      const r = i === 0 ? starR : starR;
      if (i === 0) ctx.moveTo(Math.cos(angle)*r, Math.sin(angle)*r);
      else ctx.lineTo(Math.cos(angle)*r, Math.sin(angle)*r);
    }
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  // === Subtitle overlay ===
  showSubtitle(text) {
    let el = document.getElementById('demo-subtitle');
    if (!el) return;
    if (text) {
      el.textContent = text;
      el.classList.add('show');
    } else {
      el.classList.remove('show');
    }
  }

  // === Simulated voice command display ===
  showVoiceCommand(text) {
    const transcript = document.getElementById('voiceTranscript');
    const textEl = document.getElementById('transcriptText');
    if (transcript && textEl) {
      textEl.textContent = text;
      transcript.classList.add('show');
    }
  }

  hideVoiceCommand() {
    const transcript = document.getElementById('voiceTranscript');
    if (transcript) {
      transcript.classList.remove('show');
    }
  }
}

// Export for use in oz_world.html
window.OZDemoController = OZDemoController;
