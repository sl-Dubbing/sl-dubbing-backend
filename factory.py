const API_BASE = 'https://web-production-14a1.up.railway.app';

let selectedVoice = 'source'; 
let selectedLang = 'en';
let clonedFileBase64 = null;

const ALL_LANGS = [
    {c:'en',n:'English',f:'🇺🇸'}, {c:'ar',n:'Arabic',f:'🇸🇦'}, {c:'es',n:'Spanish',f:'🇪🇸'},
    {c:'fr',n:'French',f:'🇫🇷'}, {c:'de',n:'German',f:'🇩🇪'}, {c:'it',n:'Italian',f:'🇮🇹'},
    {c:'ru',n:'Russian',f:'🇷🇺'}, {c:'zh',n:'Chinese',f:'🇨🇳'}, {c:'ja',n:'Japanese',f:'🇯🇵'}
];

document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
    renderLangs();
    loadVoices();
    
    document.getElementById('cloneFile').addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (file) {
            const reader = new FileReader();
            reader.onload = (ev) => { clonedFileBase64 = ev.target.result.split(',')[1]; };
            reader.readAsDataURL(file);
            showToast("Voice Loaded!", "#065f2c");
        }
    });
});

function renderLangs() {
    const grid = document.getElementById('langs');
    grid.innerHTML = ALL_LANGS.map(l => `
        <div class="item-card ${l.c === selectedLang ? 'active' : ''}" onclick="selectLang('${l.c}', this)">
            <span class="lang-flag">${l.f}</span>
            <span style="font-size:0.8rem;">${l.n}</span>
        </div>
    `).join('');
}

function selectLang(c, el) {
    selectedLang = c;
    document.querySelectorAll('.lang-grid .item-card').forEach(x => x.classList.remove('active'));
    el.classList.add('active');
}

function loadVoices() {
    const grid = document.getElementById('spkGrid');
    grid.innerHTML = '';

    const cloneCard = document.createElement('div');
    cloneCard.className = 'item-card active';
    cloneCard.innerHTML = `
        <i class="fas fa-check-circle chk"></i>
        <div class="voice-ai-icon clone"><i class="fas fa-wand-magic-sparkles"></i></div>
        <div class="spk-nm">Voice Clone</div>
    `;
    cloneCard.onclick = () => { setVoice('source', cloneCard); document.getElementById('cloneFile').click(); };
    grid.appendChild(cloneCard);

    const myVoices = ['muhammad', 'adam', 'bella']; 
    myVoices.forEach(name => {
        const card = document.createElement('div');
        card.className = 'item-card';
        card.innerHTML = `<i class="fas fa-check-circle chk"></i><div class="spk-av"><i class="fas fa-user"></i></div><div class="spk-nm">${name}</div>`;
        card.onclick = () => setVoice(name, card);
        grid.appendChild(card);
    });
}

function setVoice(id, el) {
    selectedVoice = id;
    document.querySelectorAll('.spk-grid .item-card').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
}

async function generateSpeech() {
    const text = document.getElementById('txt').value.trim();
    const btn = document.getElementById('generateBtn');
    if (!text) return showToast("Enter text first", "error");

    btn.disabled = true;
    btn.classList.add('loading');
    document.getElementById('progressArea').style.display = 'block';
    document.getElementById('resCard').style.display = 'none';
    document.getElementById('translatedBox').style.display = 'none';

    // 🟢 الحل السحري: المتصفح يقرأ الصوت ويرسله مشفراً ليتجنب الروابط تماماً
    let finalSampleB64 = clonedFileBase64; 

    if (selectedVoice !== 'source') {
        try {
            document.getElementById('statusTxt').innerText = 'Loading Voice Sample...';
            // قراءة الملف من مجلد samples المحلي
            const response = await fetch(`./samples/${selectedVoice}.mp3`);
            if (!response.ok) throw new Error("Voice file not found");
            const blob = await response.blob();
            finalSampleB64 = await new Promise((resolve) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                reader.readAsDataURL(blob);
            });
        } catch (e) {
            showToast(`Error loading voice: ${selectedVoice}`, "error");
            btn.disabled = false; btn.classList.remove('loading');
            return;
        }
    }

    try {
        const res = await fetch(`${API_BASE}/api/tts`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: text, 
                lang: selectedLang, 
                voice_id: selectedVoice,
                sample_b64: finalSampleB64 // نرسل الصوت كبيانات جاهزة!
            }),
            credentials: 'include'
        });
        
        if (!res.ok) throw new Error("Server error");
        const data = await res.json();
        if (!data.success) throw new Error(data.error);

        const source = new EventSource(`${API_BASE}/api/progress/${data.job_id}`);
        source.onmessage = (e) => {
            const progress = JSON.parse(e.data);
            document.getElementById('progBar').style.width = progress.progress + '%';
            document.getElementById('statusTxt').innerText = progress.message;
            document.getElementById('pctTxt').innerText = progress.progress + '%';
            
            if (progress.status === 'done') {
                source.close();
                document.getElementById('resCard').style.display = 'block';
                document.getElementById('dubAud').src = progress.audio_url;
                document.getElementById('dlBtn').href = progress.audio_url;
                
                if(progress.final_text) {
                    const tBox = document.getElementById('translatedBox');
                    tBox.style.display = 'block';
                    tBox.innerHTML = `<strong>Translated Text:</strong><br>${progress.final_text}`;
                }

                btn.disabled = false; btn.classList.remove('loading');
                showToast("Speech Ready!", "#059669");
                checkAuth();
            } else if (progress.status === 'error') {
                source.close();
                throw new Error(progress.error);
            }
        };
    } catch (err) {
        showToast(err.message, "error");
        btn.disabled = false; btn.classList.remove('loading');
    }
}

async function checkAuth() {
    try {
        const res = await fetch(API_BASE + '/api/user', { credentials: 'include' });
        const data = await res.json();
        if (data.success) {
            document.getElementById('authSection').innerHTML = `
                <div style="display:flex; gap:10px; align-items:center">
                    <div style="text-align:right">
                        <div style="font-weight:700; color:#fff">${data.user.name || 'User'}</div>
                        <div style="background:rgba(255,255,255,0.1); padding:4px 8px; border-radius:8px; font-size:0.8rem; color:#a4fec4">
                           Balance: ${data.user.credits} 💰
                        </div>
                    </div>
                    <button class="auth-btn" onclick="location.reload();">Log Out</button>
                </div>`;
        }
    } catch (e) {}
}

function showToast(msg, color) {
    const t = document.createElement('div');
    t.className = 'toast show';
    t.style.background = color === 'error' ? '#ef4444' : color;
    t.innerText = msg;
    document.getElementById('toasts').appendChild(t);
    setTimeout(() => t.remove(), 4000);
}
