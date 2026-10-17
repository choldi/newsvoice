/* OmniVoice Studio — Main Application */

// ── API Client ──────────────────────────────────────────────────────────────

const API = {
    base: '/api/v1',

    async _fetch(path, opts = {}) {
        const res = await fetch(this.base + path, opts);
        if (!res.ok) {
            const body = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(body.detail || body.error_type || `HTTP ${res.status}`);
        }
        return res;
    },

    async _json(path, opts = {}) {
        const res = await this._fetch(path, opts);
        return res.json();
    },

    async getStockVoices(language) {
        const q = language ? `?language=${language}` : '';
        return this._json(`/voices/stock${q}`);
    },

    async synthesizeTTS(params) {
        const res = await this._fetch('/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params),
        });
        return res.blob();
    },

    async synthesizeInstruct(params) {
        const res = await this._fetch('/tts/instruct', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params),
        });
        return res.blob();
    },

    async cloneVoice(name, language, audioFile, refText) {
        const fd = new FormData();
        fd.append('name', name);
        fd.append('language', language);
        fd.append('reference_audio', audioFile);
        if (refText) fd.append('ref_text', refText);
        const res = await this._fetch('/voices/clone', { method: 'POST', body: fd });
        return res.json();
    },

    async listClonedVoices(language) {
        const q = language ? `?language=${language}` : '';
        return this._json(`/voices/cloned${q}`);
    },

    async deleteClonedVoice(id) {
        await this._fetch(`/voices/cloned/${id}`, { method: 'DELETE' });
    },

    async createDesignedVoice(name, instruct, language) {
        const res = await this._fetch('/voices/design', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, instruct, language }),
        });
        return res.json();
    },

    async listDesignedVoices(language) {
        const q = language ? `?language=${language}` : '';
        return this._json(`/voices/designed${q}`);
    },

    async deleteDesignedVoice(id) {
        await this._fetch(`/voices/designed/${id}`, { method: 'DELETE' });
    },

    async generateConversation(turns, pauseMs) {
        const res = await this._fetch('/conversations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ turns, pause_ms: pauseMs }),
        });
        return res.blob();
    },

    async getEmotions() {
        return this._json('/emotions');
    },

    async getVoiceDesignTokens() {
        return this._json('/tts/voice-design/tokens');
    },

    async getHealth() {
        return this._json('/health');
    },
};

// ── Audio Player ────────────────────────────────────────────────────────────

const Player = {
    ctx: null,
    buffer: null,
    source: null,
    analyser: null,
    gain: null,
    isPlaying: false,
    startTime: 0,
    pauseOffset: 0,
    duration: 0,
    animFrame: null,
    wavBytes: null,

    _ensureCtx() {
        if (!this.ctx) {
            this.ctx = new (window.AudioContext || window.webkitAudioContext)();
            this.analyser = this.ctx.createAnalyser();
            this.analyser.fftSize = 256;
            this.gain = this.ctx.createGain();
            this.gain.connect(this.analyser);
            this.analyser.connect(this.ctx.destination);
        }
    },

    async loadFromBlob(blob) {
        this.stop();
        this._ensureCtx();
        this.wavBytes = new Uint8Array(await blob.arrayBuffer());
        const arrayBuf = this.wavBytes.buffer.slice(
            this.wavBytes.byteOffset,
            this.wavBytes.byteOffset + this.wavBytes.byteLength
        );
        this.buffer = await this.ctx.decodeAudioData(arrayBuf);
        this.duration = this.buffer.duration;
        this.pauseOffset = 0;
        this._updateTimeDisplay();
        this._drawWaveformStatic();
        document.getElementById('player-play').disabled = false;
        document.getElementById('player-stop').disabled = false;
        document.getElementById('player-download-btn').disabled = false;
    },

    play() {
        if (!this.buffer || this.isPlaying) return;
        this._ensureCtx();
        if (this.ctx.state === 'suspended') this.ctx.resume();
        this.source = this.ctx.createBufferSource();
        this.source.buffer = this.buffer;
        this.source.connect(this.gain);
        this.source.onended = () => {
            if (this.isPlaying) {
                this.isPlaying = false;
                this.pauseOffset = 0;
                this._updatePlayBtn();
                this._updateTimeDisplay();
            }
        };
        this.source.start(0, this.pauseOffset);
        this.startTime = this.ctx.currentTime - this.pauseOffset;
        this.isPlaying = true;
        this._updatePlayBtn();
        this._startWaveformAnim();
    },

    pause() {
        if (!this.isPlaying) return;
        this.pauseOffset = this.ctx.currentTime - this.startTime;
        this.source.stop();
        this.isPlaying = false;
        this._updatePlayBtn();
        cancelAnimationFrame(this.animFrame);
    },

    stop() {
        if (this.source && this.isPlaying) {
            this.source.stop();
        }
        this.isPlaying = false;
        this.pauseOffset = 0;
        this._updatePlayBtn();
        this._updateTimeDisplay();
        cancelAnimationFrame(this.animFrame);
        this._drawWaveformStatic();
    },

    toggle() {
        if (this.isPlaying) this.pause();
        else this.play();
    },

    seek(fraction) {
        if (!this.buffer) return;
        const newOffset = fraction * this.duration;
        if (this.isPlaying) {
            this.source.stop();
            this.source = this.ctx.createBufferSource();
            this.source.buffer = this.buffer;
            this.source.connect(this.gain);
            this.source.onended = () => {
                if (this.isPlaying) {
                    this.isPlaying = false;
                    this.pauseOffset = 0;
                    this._updatePlayBtn();
                }
            };
            this.source.start(0, newOffset);
            this.startTime = this.ctx.currentTime - newOffset;
        } else {
            this.pauseOffset = newOffset;
        }
        this._updateTimeDisplay();
    },

    setVolume(v) {
        if (this.gain) this.gain.gain.value = v;
    },

    getCurrentTime() {
        if (!this.buffer) return 0;
        if (this.isPlaying) return this.ctx.currentTime - this.startTime;
        return this.pauseOffset;
    },

    _updatePlayBtn() {
        const playIcon = document.getElementById('player-play-icon');
        const pauseIcon = document.getElementById('player-pause-icon');
        playIcon.classList.toggle('hidden', this.isPlaying);
        pauseIcon.classList.toggle('hidden', !this.isPlaying);
    },

    _updateTimeDisplay() {
        document.getElementById('player-current').textContent = fmtTime(this.getCurrentTime());
        document.getElementById('player-total').textContent = fmtTime(this.duration);
        const pct = this.duration > 0 ? (this.getCurrentTime() / this.duration) * 100 : 0;
        document.getElementById('player-progress').style.width = pct + '%';
    },

    _startWaveformAnim() {
        const canvas = document.getElementById('player-waveform');
        const ctx2d = canvas.getContext('2d');
        const w = canvas.width;
        const h = canvas.height;
        const data = new Uint8Array(this.analyser.frequencyBinCount);

        const draw = () => {
            this.animFrame = requestAnimationFrame(draw);
            this.analyser.getByteFrequencyData(data);
            ctx2d.clearRect(0, 0, w, h);

            const barW = 2;
            const gap = 1;
            const bars = Math.floor(w / (barW + gap));
            for (let i = 0; i < bars; i++) {
                const idx = Math.floor(i * data.length / bars);
                const val = data[idx] / 255;
                const barH = Math.max(2, val * h);
                const x = i * (barW + gap);
                const y = (h - barH) / 2;
                ctx2d.fillStyle = `rgba(139, 92, 246, ${0.4 + val * 0.6})`;
                ctx2d.fillRect(x, y, barW, barH);
            }
            this._updateTimeDisplay();
        };
        draw();
    },

    _drawWaveformStatic() {
        const canvas = document.getElementById('player-waveform');
        const ctx2d = canvas.getContext('2d');
        const w = canvas.width;
        const h = canvas.height;
        ctx2d.clearRect(0, 0, w, h);

        if (!this.buffer) {
            ctx2d.fillStyle = '#3f3f46';
            ctx2d.font = '11px sans-serif';
            ctx2d.textAlign = 'center';
            ctx2d.fillText('No audio loaded', w / 2, h / 2 + 4);
            return;
        }

        const data = this.buffer.getChannelData(0);
        const step = Math.max(1, Math.floor(data.length / w));
        ctx2d.strokeStyle = '#8b5cf6';
        ctx2d.lineWidth = 1;
        ctx2d.beginPath();
        for (let i = 0; i < w; i++) {
            const idx = Math.floor(i * data.length / w);
            let min = 1, max = -1;
            for (let j = 0; j < step; j++) {
                const val = data[idx + j] || 0;
                if (val < min) min = val;
                if (val > max) max = val;
            }
            const y1 = (1 + min) * h / 2;
            const y2 = (1 + max) * h / 2;
            ctx2d.moveTo(i, y1);
            ctx2d.lineTo(i, y2);
        }
        ctx2d.stroke();
    },
};

// ── Format Converter ────────────────────────────────────────────────────────

const FormatConverter = {
    toMP3(wavBytes) {
        // Decode WAV to PCM samples
        const view = new DataView(wavBytes.buffer, wavBytes.byteOffset, wavBytes.byteLength);
        const sampleRate = view.getUint32(24, true);
        const bitsPerSample = view.getUint32(22, true) >> 16;
        // Find data chunk
        let dataOffset = 44;
        while (dataOffset < wavBytes.length - 8) {
            if (wavBytes[dataOffset] === 0x64 && wavBytes[dataOffset+1] === 0x61 &&
                wavBytes[dataOffset+2] === 0x74 && wavBytes[dataOffset+3] === 0x61) {
                break;
            }
            dataOffset++;
        }
        dataOffset += 8;
        const dataSize = view.getUint32(dataOffset - 4, true);
        const numSamples = Math.floor(dataSize / 2);
        const samples = new Int16Array(numSamples);
        for (let i = 0; i < numSamples; i++) {
            samples[i] = view.getInt16(dataOffset + i * 2, true);
        }

        const mp3enc = new lamejs.Mp3Encoder(1, sampleRate, 192);
        const blockSize = 1152;
        const mp3Data = [];
        for (let i = 0; i < samples.length; i += blockSize) {
            const chunk = samples.subarray(i, i + blockSize);
            const buf = mp3enc.encodeBuffer(chunk);
            if (buf.length > 0) mp3Data.push(new Int8Array(buf));
        }
        const tail = mp3enc.flush();
        if (tail.length > 0) mp3Data.push(new Int8Array(tail));

        const totalLen = mp3Data.reduce((a, b) => a + b.length, 0);
        const result = new Uint8Array(totalLen);
        let offset = 0;
        for (const chunk of mp3Data) {
            result.set(chunk, offset);
            offset += chunk.length;
        }
        return new Blob([result], { type: 'audio/mpeg' });
    },

    async toOGG(wavBlob) {
        // Use MediaRecorder API with opus codec
        try {
            const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            const arrayBuf = await wavBlob.arrayBuffer();
            const audioBuf = await audioCtx.decodeAudioData(arrayBuf);

            // Render to offline context for MediaRecorder
            const dest = audioCtx.createMediaStreamDestination();
            const source = audioCtx.createBufferSource();
            source.buffer = audioBuf;
            source.connect(dest);
            source.start();

            return new Promise((resolve) => {
                const chunks = [];
                const mr = new MediaRecorder(dest.stream, { mimeType: 'audio/webm;codecs=opus' });
                mr.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };
                mr.onstop = () => {
                    audioCtx.close();
                    resolve(new Blob(chunks, { type: 'audio/ogg' }));
                };
                mr.start();
                setTimeout(() => mr.stop(), (audioBuf.duration * 1000) + 200);
            });
        } catch (e) {
            // Fallback: return WAV with note
            toast('OGG encoding not supported in this browser. Downloading as WAV instead.', 'info');
            return wavBlob;
        }
    },

    async toM4A(wavBlob) {
        // Browser-native: use MediaRecorder with mp4 if available
        try {
            const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            const arrayBuf = await wavBlob.arrayBuffer();
            const audioBuf = await audioCtx.decodeAudioData(arrayBuf);
            const dest = audioCtx.createMediaStreamDestination();
            const source = audioCtx.createBufferSource();
            source.buffer = audioBuf;
            source.connect(dest);
            source.start();

            const mimeType = MediaRecorder.isTypeSupported('audio/mp4;codecs=mp4a.40.2')
                ? 'audio/mp4;codecs=mp4a.40.2'
                : 'audio/webm;codecs=aac';

            return new Promise((resolve) => {
                const chunks = [];
                const mr = new MediaRecorder(dest.stream, { mimeType });
                mr.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };
                mr.onstop = () => {
                    audioCtx.close();
                    const ext = mimeType.includes('mp4') ? 'm4a' : 'webm';
                    resolve(new Blob(chunks, { type: `audio/${ext}` }));
                };
                mr.start();
                setTimeout(() => mr.stop(), (audioBuf.duration * 1000) + 200);
            });
        } catch (e) {
            toast('M4A encoding not supported. Downloading as WAV.', 'info');
            return wavBlob;
        }
    },

    download(blob, filename) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
    },
};

// ── UI State ────────────────────────────────────────────────────────────────

const LANGUAGES = [
    { code: 'es', name: 'Spanish' }, { code: 'en', name: 'English' },
    { code: 'fr', name: 'French' }, { code: 'de', name: 'German' },
    { code: 'it', name: 'Italian' }, { code: 'pt', name: 'Portuguese' },
    { code: 'zh', name: 'Chinese' }, { code: 'ja', name: 'Japanese' },
    { code: 'ko', name: 'Korean' },
];

const EMOTION_META = {
    happy: { icon: '😊', color: '#fbbf24' }, sad: { icon: '😢', color: '#60a5fa' },
    angry: { icon: '😠', color: '#ef4444' }, excited: { icon: '🤩', color: '#f97316' },
    calm: { icon: '😌', color: '#34d399' }, nervous: { icon: '😰', color: '#a78bfa' },
    whisper: { icon: '🤫', color: '#94a3b8' }, singing: { icon: '🎵', color: '#f472b6' },
};

let state = {
    stockVoices: [],
    emotions: [],
    designTokens: {},
    selectedEmotion: { tts: null, design: null },
    clonedVoices: [],
    designedVoices: [],
    convoTurns: [],
    currentBlob: null,
    currentFormat: 'wav',
};

// ── Helpers ─────────────────────────────────────────────────────────────────

function fmtTime(s) {
    if (!s || !isFinite(s)) return '0:00';
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return `${m}:${sec.toString().padStart(2, '0')}`;
}

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function toast(msg, type = 'info') {
    const container = $('#toast-container');
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => el.remove(), 4000);
}

function populateLangSelect(sel) {
    LANGUAGES.forEach(l => {
        const opt = document.createElement('option');
        opt.value = l.code;
        opt.textContent = `${l.name} (${l.code})`;
        sel.appendChild(opt);
    });
}

function renderEmotionButtons(container, stateKey) {
    container.innerHTML = '';
    const noneBtn = document.createElement('button');
    noneBtn.className = 'emotion-btn active';
    noneBtn.textContent = 'None';
    noneBtn.onclick = () => {
        state.selectedEmotion[stateKey] = null;
        container.querySelectorAll('.emotion-btn').forEach(b => b.classList.remove('active'));
        noneBtn.classList.add('active');
    };
    container.appendChild(noneBtn);

    state.emotions.forEach(e => {
        const meta = EMOTION_META[e.id] || {};
        const btn = document.createElement('button');
        btn.className = 'emotion-btn';
        btn.textContent = `${meta.icon || ''} ${e.name}`;
        btn.onclick = () => {
            state.selectedEmotion[stateKey] = e.id;
            container.querySelectorAll('.emotion-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        };
        container.appendChild(btn);
    });
}

function setLoading(btn, loading) {
    if (loading) {
        btn.dataset.origText = btn.innerHTML;
        btn.innerHTML = '<span class="spinner"></span>';
        btn.disabled = true;
    } else {
        btn.innerHTML = btn.dataset.origText || btn.innerHTML;
        btn.disabled = false;
    }
}

function voiceDisplayName(v) {
    return v.name || v.voice_id;
}

// ── Tab Navigation ──────────────────────────────────────────────────────────

function initTabs() {
    $$('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;
            $$('.nav-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            $$('.tab-content').forEach(s => s.classList.add('hidden'));
            $(`#tab-${tab}`).classList.remove('hidden');
            // Close sidebar on mobile
            $('#sidebar').classList.add('-translate-x-full');
            // Refresh data for certain tabs
            if (tab === 'library') refreshLibrary();
            if (tab === 'clone') refreshClonedVoices();
        });
    });

    // Sidebar toggle (mobile)
    $('#sidebar-toggle').addEventListener('click', () => {
        $('#sidebar').classList.toggle('-translate-x-full');
    });
}

// ── TTS Section ─────────────────────────────────────────────────────────────

async function initTTS() {
    const langSel = $('#tts-language');
    populateLangSelect(langSel);
    langSel.value = 'es';

    langSel.addEventListener('change', () => refreshTTSVoices());
    $('#tts-gender-male').addEventListener('change', () => refreshTTSVoices());
    $('#tts-gender-female').addEventListener('change', () => refreshTTSVoices());
    $('#tts-voice').addEventListener('change', () => updateTTSVoiceInfo());
    $('#tts-speed').addEventListener('input', (e) => {
        $('#tts-speed-val').textContent = e.target.value;
    });

    $('#tts-synthesize').addEventListener('click', doTTSSynthesize);

    await refreshTTSVoices();
}

async function refreshTTSVoices() {
    try {
        const lang = $('#tts-language').value;
        state.stockVoices = await API.getStockVoices(lang);
        const sel = $('#tts-voice');
        sel.innerHTML = '';
        const male = $('#tts-gender-male').checked;
        const female = $('#tts-gender-female').checked;
        const filtered = state.stockVoices.filter(v => {
            if (v.gender === 'male' && !male) return false;
            if (v.gender === 'female' && !female) return false;
            return true;
        });
        filtered.forEach(v => {
            const opt = document.createElement('option');
            opt.value = v.voice_id;
            opt.textContent = `${v.name} (${v.gender})`;
            sel.appendChild(opt);
        });
        // Append cloned voices for the selected language
        try {
            state.clonedVoices = await API.listClonedVoices(lang);
            state.clonedVoices.forEach(v => {
                const opt = document.createElement('option');
                opt.value = v.id;
                opt.textContent = `${v.name} (cloned)`;
                sel.appendChild(opt);
            });
        } catch (_) { /* cloned voices optional */ }
        // Append designed voices for the selected language
        try {
            state.designedVoices = await API.listDesignedVoices(lang);
            state.designedVoices.forEach(v => {
                const opt = document.createElement('option');
                opt.value = v.id;
                opt.textContent = `${v.name} (designed)`;
                sel.appendChild(opt);
            });
        } catch (_) { /* designed voices optional */ }
        updateTTSVoiceInfo();
    } catch (e) {
        toast('Failed to load voices: ' + e.message, 'error');
    }
}

function updateTTSVoiceInfo() {
    const id = $('#tts-voice').value;
    const voice = state.stockVoices.find(v => v.voice_id === id);
    const cloned = (state.clonedVoices || []).find(v => v.id === id);
    const designed = (state.designedVoices || []).find(v => v.id === id);
    const info = $('#tts-voice-info');
    if (voice) {
        info.innerHTML = `
            <div class="space-y-1">
                <div><span class="text-zinc-500">ID:</span> <span class="font-mono text-brand-400">${voice.voice_id}</span></div>
                <div><span class="text-zinc-500">Language:</span> ${voice.language}</div>
                <div><span class="text-zinc-500">Gender:</span> ${voice.gender}</div>
            </div>`;
    } else if (cloned) {
        info.innerHTML = `
            <div class="space-y-1">
                <div><span class="text-zinc-500">ID:</span> <span class="font-mono text-brand-400">${cloned.id}</span></div>
                <div><span class="text-zinc-500">Language:</span> ${cloned.language}</div>
                <div><span class="text-zinc-500">Type:</span> Cloned voice</div>
            </div>`;
    } else if (designed) {
        info.innerHTML = `
            <div class="space-y-1">
                <div><span class="text-zinc-500">ID:</span> <span class="font-mono text-brand-400">${designed.id}</span></div>
                <div><span class="text-zinc-500">Language:</span> ${designed.language}</div>
                <div><span class="text-zinc-500">Instruct:</span> ${designed.instruct}</div>
            </div>`;
    } else {
        info.textContent = 'Select a voice';
    }
}

async function doTTSSynthesize() {
    const text = $('#tts-text').value.trim();
    if (!text) { toast('Enter some text first', 'error'); return; }
    const voiceId = $('#tts-voice').value;
    if (!voiceId) { toast('Select a voice', 'error'); return; }

    const btn = $('#tts-synthesize');
    setLoading(btn, true);
    try {
        const params = {
            text,
            voice_id: voiceId,
            language: $('#tts-language').value,
            speed: parseFloat($('#tts-speed').value),
            num_step: parseInt($('#tts-num-step').value) || 32,
            denoise: $('#tts-denoise').checked,
            guidance_scale: parseFloat($('#tts-guidance').value) || 2.0,
            postprocess_output: $('#tts-postprocess').checked,
            preprocess_prompt: $('#tts-preprocess').checked,
        };
        if (state.selectedEmotion.tts) params.emotion = state.selectedEmotion.tts;
        const dur = parseFloat($('#tts-duration').value);
        if (dur > 0) params.duration = dur;

        const blob = await API.synthesizeTTS(params);
        await Player.loadFromBlob(blob);
        state.currentBlob = blob;
        state.currentFormat = 'wav';
        Player.play();
        toast('Synthesis complete!', 'success');
    } catch (e) {
        toast('Synthesis failed: ' + e.message, 'error');
    } finally {
        setLoading(btn, false);
    }
}

// ── Voice Design Section ────────────────────────────────────────────────────

async function initDesign() {
    const langSel = $('#design-language');
    populateLangSelect(langSel);
    langSel.value = 'en';

    $('#design-speed').addEventListener('input', (e) => {
        $('#design-speed-val').textContent = e.target.value;
    });

    $('#design-synthesize').addEventListener('click', doDesignSynthesize);
    $('#design-save').addEventListener('click', () => {
        const dlg = $('#design-save-dialog');
        dlg.classList.toggle('hidden');
    });
    $('#design-save-confirm').addEventListener('click', doDesignSave);

    try {
        state.designTokens = await API.getVoiceDesignTokens();
        renderDesignTokens();
    } catch (e) {
        toast('Failed to load voice design tokens', 'error');
    }

    renderEmotionButtons($('#design-emotions'), 'design');
}

function renderDesignTokens() {
    const container = $('#design-tokens');
    container.innerHTML = '';
    const categoryLabels = {
        gender: 'Gender', age: 'Age', pitch: 'Pitch', style: 'Style',
        english_accent: 'English Accent', chinese_dialect: 'Chinese Dialect',
    };
    for (const [cat, tokens] of Object.entries(state.designTokens)) {
        const group = document.createElement('div');
        group.className = 'token-group';
        group.innerHTML = `<div class="token-group-title">${categoryLabels[cat] || cat}</div>`;
        tokens.forEach(token => {
            const label = document.createElement('label');
            label.className = 'token-checkbox';
            label.innerHTML = `<input type="checkbox" data-category="${cat}" value="${token}"> ${token}`;
            label.querySelector('input').addEventListener('change', updateInstructPreview);
            group.appendChild(label);
        });
        container.appendChild(group);
    }
}

function updateInstructPreview() {
    const checked = $$('#design-tokens input[type="checkbox"]:checked');
    const parts = Array.from(checked).map(cb => cb.value);
    const preview = $('#design-instruct-preview');
    preview.textContent = parts.length > 0 ? parts.join(', ') : 'Select attributes...';
}

function buildInstruct() {
    const checked = $$('#design-tokens input[type="checkbox"]:checked');
    return Array.from(checked).map(cb => cb.value).join(', ');
}

async function doDesignSynthesize() {
    const text = $('#design-text').value.trim();
    if (!text) { toast('Enter some text', 'error'); return; }
    const instruct = buildInstruct();
    if (!instruct) { toast('Select at least one attribute', 'error'); return; }

    const btn = $('#design-synthesize');
    setLoading(btn, true);
    try {
        const params = {
            text,
            instruct,
            language: $('#design-language').value,
            speed: parseFloat($('#design-speed').value),
        };
        if (state.selectedEmotion.design) params.emotion = state.selectedEmotion.design;

        const blob = await API.synthesizeInstruct(params);
        await Player.loadFromBlob(blob);
        state.currentBlob = blob;
        Player.play();
        toast('Synthesis complete!', 'success');
    } catch (e) {
        toast('Synthesis failed: ' + e.message, 'error');
    } finally {
        setLoading(btn, false);
    }
}

async function doDesignSave() {
    const name = $('#design-preset-name').value.trim();
    if (!name) { toast('Enter a name', 'error'); return; }
    const instruct = buildInstruct();
    if (!instruct) { toast('Select attributes first', 'error'); return; }

    try {
        await API.createDesignedVoice(name, instruct, $('#design-language').value);
        toast(`Preset "${name}" saved!`, 'success');
        $('#design-save-dialog').classList.add('hidden');
        $('#design-preset-name').value = '';
    } catch (e) {
        toast('Save failed: ' + e.message, 'error');
    }
}

// ── Voice Cloning Section ───────────────────────────────────────────────────

let cloneFile = null;

function initClone() {
    populateLangSelect($('#clone-language'));

    const dropzone = $('#clone-dropzone');
    const fileInput = $('#clone-file');

    dropzone.addEventListener('click', () => fileInput.click());
    dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('dragover');
        if (e.dataTransfer.files.length) handleCloneFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener('change', () => {
        if (fileInput.files.length) handleCloneFile(fileInput.files[0]);
    });

    $('#clone-submit').addEventListener('click', doCloneVoice);
    $('#clone-test-btn').addEventListener('click', doCloneTest);

    refreshClonedVoices();
}

function handleCloneFile(file) {
    cloneFile = file;
    const preview = $('#clone-audio-preview');
    const audio = $('#clone-audio');
    const info = $('#clone-file-info');

    const url = URL.createObjectURL(file);
    audio.src = url;
    preview.classList.remove('hidden');
    info.textContent = `${file.name} — ${(file.size / 1024).toFixed(1)} KB`;
    $('#clone-submit').disabled = false;
}

async function doCloneVoice() {
    if (!cloneFile) { toast('Upload a reference audio file first', 'error'); return; }
    const name = $('#clone-name').value.trim();
    if (!name) { toast('Enter a name', 'error'); return; }

    const btn = $('#clone-submit');
    setLoading(btn, true);
    try {
        const result = await API.cloneVoice(name, $('#clone-language').value, cloneFile, $('#clone-ref-text').value.trim() || null);
        toast(`Voice "${name}" cloned! ID: ${result.voice_id}`, 'success');
        $('#clone-name').value = '';
        $('#clone-ref-text').value = '';
        cloneFile = null;
        $('#clone-audio-preview').classList.add('hidden');
        btn.disabled = true;
        $('#clone-test-btn').disabled = false;
        await refreshClonedVoices();
    } catch (e) {
        toast('Clone failed: ' + e.message, 'error');
    } finally {
        setLoading(btn, false);
    }
}

async function doCloneTest() {
    const text = $('#clone-test-text').value.trim();
    if (!text) { toast('Enter test text', 'error'); return; }
    const voice = state.clonedVoices[0];
    if (!voice) { toast('No cloned voices available', 'error'); return; }

    const btn = $('#clone-test-btn');
    setLoading(btn, true);
    try {
        const blob = await API.synthesizeTTS({
            text,
            voice_id: voice.id,
            language: voice.language,
        });
        await Player.loadFromBlob(blob);
        state.currentBlob = blob;
        Player.play();
        toast('Test synthesis complete!', 'success');
    } catch (e) {
        toast('Test failed: ' + e.message, 'error');
    } finally {
        setLoading(btn, false);
    }
}

async function refreshClonedVoices() {
    try {
        state.clonedVoices = await API.listClonedVoices();
        renderClonedVoices();
    } catch (e) { /* ignore */ }
}

function renderClonedVoices() {
    const container = $('#clone-voices-list');
    if (!state.clonedVoices.length) {
        container.innerHTML = '<p class="text-sm text-zinc-500 text-center py-4">No cloned voices yet</p>';
        return;
    }
    container.innerHTML = state.clonedVoices.map(v => `
        <div class="voice-card flex items-center justify-between">
            <div class="min-w-0">
                <div class="text-sm font-medium truncate">${v.name}</div>
                <div class="text-xs text-zinc-500">${v.language} — ${v.duration_sec?.toFixed(1) || '?'}s</div>
            </div>
            <div class="flex gap-1 shrink-0 ml-2">
                <button class="clone-test-one text-xs bg-zinc-700 hover:bg-zinc-600 px-2 py-1 rounded" data-id="${v.id}" data-lang="${v.language}">Test</button>
                <button class="clone-delete-one text-xs bg-red-900/50 hover:bg-red-800 px-2 py-1 rounded" data-id="${v.id}">Del</button>
            </div>
        </div>
    `).join('');

    container.querySelectorAll('.clone-test-one').forEach(btn => {
        btn.addEventListener('click', async () => {
            const text = 'Hello, this is a test of the cloned voice.';
            try {
                const blob = await API.synthesizeTTS({ text, voice_id: btn.dataset.id, language: btn.dataset.lang });
                await Player.loadFromBlob(blob);
                state.currentBlob = blob;
                Player.play();
            } catch (e) { toast('Test failed: ' + e.message, 'error'); }
        });
    });

    container.querySelectorAll('.clone-delete-one').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('Delete this cloned voice?')) return;
            try {
                await API.deleteClonedVoice(btn.dataset.id);
                toast('Voice deleted', 'success');
                await refreshClonedVoices();
            } catch (e) { toast('Delete failed: ' + e.message, 'error'); }
        });
    });
}

// ── Conversations Section ───────────────────────────────────────────────────

function initConversations() {
    addConvoTurn();
    addConvoTurn();
    $('#convo-add-turn').addEventListener('click', addConvoTurn);
    $('#convo-pause').addEventListener('input', (e) => {
        $('#convo-pause-val').textContent = e.target.value;
    });
    $('#convo-generate').addEventListener('click', doConvoGenerate);
}

function addConvoTurn() {
    const id = Date.now();
    const container = $('#convo-turns');
    const turn = document.createElement('div');
    turn.className = 'flex gap-2 items-start';
    turn.dataset.turnId = id;
    turn.innerHTML = `
        <select class="convo-voice bg-zinc-800 border border-zinc-700 rounded-lg px-2 py-2 text-sm focus:ring-2 focus:ring-brand-500 w-48 shrink-0"></select>
        <input type="text" class="convo-text flex-1 bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brand-500" placeholder="Dialogue text...">
        <button class="convo-remove text-zinc-500 hover:text-red-400 px-2 py-2 shrink-0">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
    `;
    container.appendChild(turn);

    // Populate voices (stock + cloned + designed)
    const sel = turn.querySelector('.convo-voice');
    state.stockVoices.forEach(v => {
        const opt = document.createElement('option');
        opt.value = v.voice_id;
        opt.dataset.lang = v.language;
        opt.textContent = v.name;
        sel.appendChild(opt);
    });
    (state.clonedVoices || []).forEach(v => {
        const opt = document.createElement('option');
        opt.value = v.id;
        opt.dataset.lang = v.language;
        opt.textContent = `${v.name} (cloned)`;
        sel.appendChild(opt);
    });
    (state.designedVoices || []).forEach(v => {
        const opt = document.createElement('option');
        opt.value = v.id;
        opt.dataset.lang = v.language;
        opt.textContent = `${v.name} (designed)`;
        sel.appendChild(opt);
    });

    turn.querySelector('.convo-remove').addEventListener('click', () => {
        if ($('#convo-turns').children.length > 2) {
            turn.remove();
        } else {
            toast('Minimum 2 turns required', 'info');
        }
    });
}

async function doConvoGenerate() {
    const turns = [];
    for (const el of $('#convo-turns').children) {
        const voiceSel = el.querySelector('.convo-voice');
        const voiceId = voiceSel.value;
        const text = el.querySelector('.convo-text').value.trim();
        if (!text) { toast('All turns must have text', 'error'); return; }
        const lang = voiceSel.selectedOptions[0]?.dataset.lang || 'es';
        turns.push({ voice_id: voiceId, text, language: lang });
    }
    if (turns.length < 2) { toast('Minimum 2 turns', 'error'); return; }

    const btn = $('#convo-generate');
    setLoading(btn, true);
    try {
        const blob = await API.generateConversation(turns, parseInt($('#convo-pause').value));
        await Player.loadFromBlob(blob);
        state.currentBlob = blob;
        Player.play();
        toast('Conversation generated!', 'success');
    } catch (e) {
        toast('Generation failed: ' + e.message, 'error');
    } finally {
        setLoading(btn, false);
    }
}

// ── Voice Library Section ───────────────────────────────────────────────────

function initLibrary() {
    $$('.lib-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            $$('.lib-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            $$('.lib-content').forEach(c => c.classList.add('hidden'));
            $(`#lib-${tab.dataset.libTab}`).classList.remove('hidden');
        });
    });
}

async function refreshLibrary() {
    try {
        state.stockVoices = await API.getStockVoices();
        state.clonedVoices = await API.listClonedVoices();
        state.designedVoices = await API.listDesignedVoices();
    } catch (e) { /* ignore */ }
    renderLibraryStock();
    renderLibraryCloned();
    renderLibraryDesigned();
}

function renderLibraryStock() {
    const grid = $('#lib-stock-grid');
    grid.innerHTML = state.stockVoices.map(v => `
        <div class="voice-card">
            <div class="flex justify-between items-start">
                <div>
                    <div class="text-sm font-medium">${v.name}</div>
                    <div class="text-xs text-zinc-500">${v.voice_id}</div>
                </div>
                <span class="text-xs px-1.5 py-0.5 rounded ${v.gender === 'male' ? 'bg-blue-900/50 text-blue-300' : 'bg-pink-900/50 text-pink-300'}">${v.gender}</span>
            </div>
            <div class="mt-2 text-xs text-zinc-500">Lang: ${v.language}</div>
        </div>
    `).join('');
}

function renderLibraryCloned() {
    const container = $('#lib-cloned');
    if (!state.clonedVoices.length) {
        container.innerHTML = '<p class="text-sm text-zinc-500">No cloned voices</p>';
        return;
    }
    container.innerHTML = state.clonedVoices.map(v => `
        <div class="voice-card mb-2 flex justify-between items-center">
            <div>
                <div class="text-sm font-medium">${v.name}</div>
                <div class="text-xs text-zinc-500">${v.language} — ${v.duration_sec?.toFixed(1) || '?'}s — ${v.id.slice(0, 8)}...</div>
            </div>
            <button class="lib-delete-cloned text-xs bg-red-900/50 hover:bg-red-800 px-2 py-1 rounded" data-id="${v.id}">Delete</button>
        </div>
    `).join('');

    container.querySelectorAll('.lib-delete-cloned').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('Delete?')) return;
            try {
                await API.deleteClonedVoice(btn.dataset.id);
                toast('Deleted', 'success');
                await refreshLibrary();
            } catch (e) { toast('Delete failed', 'error'); }
        });
    });
}

function renderLibraryDesigned() {
    const container = $('#lib-designed');
    if (!state.designedVoices.length) {
        container.innerHTML = '<p class="text-sm text-zinc-500">No designed voices</p>';
        return;
    }
    container.innerHTML = state.designedVoices.map(v => `
        <div class="voice-card mb-2 flex justify-between items-center">
            <div>
                <div class="text-sm font-medium">${v.name}</div>
                <div class="text-xs text-zinc-500 font-mono">${v.instruct}</div>
                <div class="text-xs text-zinc-500">${v.language}</div>
            </div>
            <div class="flex gap-1">
                <button class="lib-use-designed text-xs bg-brand-600 hover:bg-brand-700 px-2 py-1 rounded" data-instruct="${encodeURIComponent(v.instruct)}" data-lang="${v.language}">Use</button>
                <button class="lib-delete-designed text-xs bg-red-900/50 hover:bg-red-800 px-2 py-1 rounded" data-id="${v.id}">Del</button>
            </div>
        </div>
    `).join('');

    container.querySelectorAll('.lib-delete-designed').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('Delete?')) return;
            try {
                await API.deleteDesignedVoice(btn.dataset.id);
                toast('Deleted', 'success');
                await refreshLibrary();
            } catch (e) { toast('Delete failed', 'error'); }
        });
    });

    container.querySelectorAll('.lib-use-designed').forEach(btn => {
        btn.addEventListener('click', async () => {
            const instruct = decodeURIComponent(btn.dataset.instruct);
            const lang = btn.dataset.lang;
            const text = prompt('Enter text to synthesize with this voice:');
            if (!text) return;
            try {
                const blob = await API.synthesizeInstruct({ text, instruct, language: lang });
                await Player.loadFromBlob(blob);
                state.currentBlob = blob;
                Player.play();
            } catch (e) { toast('Synthesis failed: ' + e.message, 'error'); }
        });
    });
}

// ── Player Controls ─────────────────────────────────────────────────────────

function initPlayer() {
    $('#player-play').addEventListener('click', () => Player.toggle());
    $('#player-stop').addEventListener('click', () => Player.stop());
    $('#player-volume').addEventListener('input', (e) => Player.setVolume(parseFloat(e.target.value)));

    // Waveform seek
    $('#player-waveform').addEventListener('click', (e) => {
        const rect = e.target.getBoundingClientRect();
        const fraction = (e.clientX - rect.left) / rect.width;
        Player.seek(Math.max(0, Math.min(1, fraction)));
    });

    // Download menu
    const dlBtn = $('#player-download-btn');
    const dlMenu = $('#player-download-menu');
    dlBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        dlMenu.classList.toggle('hidden');
    });
    document.addEventListener('click', () => dlMenu.classList.add('hidden'));

    $$('.dl-option').forEach(opt => {
        opt.addEventListener('click', async (e) => {
            e.stopPropagation();
            dlMenu.classList.add('hidden');
            const format = opt.dataset.format;
            if (!state.currentBlob) { toast('No audio to download', 'error'); return; }

            const btn = dlBtn;
            setLoading(btn, true);
            try {
                let blob;
                const ext = { wav: 'wav', mp3: 'mp3', ogg: 'ogg', m4a: 'm4a' }[format] || 'wav';
                switch (format) {
                    case 'wav': blob = state.currentBlob; break;
                    case 'mp3': blob = FormatConverter.toMP3(Player.wavBytes); break;
                    case 'ogg': blob = await FormatConverter.toOGG(state.currentBlob); break;
                    case 'm4a': blob = await FormatConverter.toM4A(state.currentBlob); break;
                }
                FormatConverter.download(blob, `omnivoice_output.${ext}`);
                toast(`Downloaded as ${ext.toUpperCase()}`, 'success');
            } catch (e) {
                toast('Download failed: ' + e.message, 'error');
            } finally {
                setLoading(btn, false);
            }
        });
    });

    // Keyboard shortcut
    document.addEventListener('keydown', (e) => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
        if (e.code === 'Space') { e.preventDefault(); Player.toggle(); }
    });
}

// ── Health Badge ────────────────────────────────────────────────────────────

async function updateHealth() {
    try {
        const h = await API.getHealth();
        const dot = $('#health-dot');
        const txt = $('#health-text');
        if (h.status === 'ok') {
            dot.className = 'w-2 h-2 rounded-full bg-green-500';
            txt.textContent = `${h.mode} — ${h.device}`;
        } else {
            dot.className = 'w-2 h-2 rounded-full bg-yellow-500';
            txt.textContent = `${h.status} — ${h.mode}`;
        }
    } catch {
        const dot = $('#health-dot');
        const txt = $('#health-text');
        dot.className = 'w-2 h-2 rounded-full bg-red-500';
        txt.textContent = 'Offline';
    }
}

// ── Canvas Sizing ───────────────────────────────────────────────────────────

function resizeCanvases() {
    const canvas = $('#player-waveform');
    if (canvas) {
        const rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width;
        canvas.height = 40;
        Player._drawWaveformStatic();
    }
}

// ── Init ────────────────────────────────────────────────────────────────────

async function init() {
    initTabs();
    initPlayer();
    initLibrary();

    // Load emotions first (needed by multiple sections)
    try {
        state.emotions = await API.getEmotions();
    } catch (e) { /* continue without emotions */ }

    renderEmotionButtons($('#tts-emotions'), 'tts');

    // Init all sections
    await Promise.all([initTTS(), initDesign()]);
    initClone();
    initConversations();

    // Health check
    await updateHealth();
    setInterval(updateHealth, 30000);

    // Canvas sizing
    resizeCanvases();
    window.addEventListener('resize', resizeCanvases);

    // Update cloned voices list test button state
    setTimeout(() => {
        if (state.clonedVoices.length > 0) {
            $('#clone-test-btn').disabled = false;
        }
    }, 1000);
}

document.addEventListener('DOMContentLoaded', init);
