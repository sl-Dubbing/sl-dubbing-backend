// script.js
const API_BASE = 'https://web-production-14a1.up.railway.app';
const GITHUB_USER = "sl-Dubbing"; 
const REPO_NAME = "sl-dubbing-frontend";

let selectedVoice = 'source';
let selectedLang = 'ar';
let currentJobId = null;
let pollInterval = null;

document.addEventListener('DOMContentLoaded', () => {
    loadVoicesFromGithub();
    checkAuth();
    
    const langGrid = document.getElementById('langGrid');
    if (langGrid) {
        const langs = ['ar','en','es','fr','de','it','pt','tr','ru','zh','ja','ko','hi'];
        langGrid.innerHTML = '';
        langs.forEach(l => {
            const el = document.createElement('div');
            el.className = 'lang-box' + (l === selectedLang ? ' active' : '');
            el.innerText = l.toUpperCase();
            el.onclick = () => {
                document.querySelectorAll('.lang-box').forEach(n => n.classList.remove('active'));
                el.classList.add('active');
                selectedLang = l;
            };
            langGrid.appendChild(el);
        });
    }

    const srtFile = document.getElementById('srtFile');
    if (srtFile) {
        srtFile.addEventListener('change', () => {
            if (srtFile.files.length) {
                document.getElementById('srtZone').innerText = srtFile.files[0].name;
                document.getElementById('srtZone').classList.add('ok');
            }
        });
    }
});

async function loadVoicesFromGithub() {
    const spkGrid = document.getElementById('spkGrid');
    if (!spkGrid) return;
    spkGrid.innerHTML = '';

    const sourceCard = document.createElement('div');
    sourceCard.className = 'spk-card active';
    sourceCard.innerHTML = `<i class="fas fa-check-circle chk"></i><div class="spk-av">S</div><div class="spk-nm">صوت المصدر</div>`;
    sourceCard.onclick = () => selectVoice('source', sourceCard);
    spkGrid.appendChild(sourceCard);

    try {
        const url = `https://api.github.com/repos/${GITHUB_USER}/${REPO_NAME}/contents/samples?t=${Date.now()}`;
        const res = await fetch(url);
        const files = await res.json();
        files.filter(f => f.name.toLowerCase().endsWith('.mp3')).forEach(file => {
            const name = file.name.replace(/\.[^/.]+$/, "");
            const card = document.createElement('div');
            card.className = 'spk-card';
            card.innerHTML = `<i class="fas fa-check-circle chk"></i><div class="spk-av">${name[0].toUpperCase()}</div><div class="spk-nm">${name}</div>`;
            card.onclick = () => selectVoice(name, card);
            spkGrid.appendChild(card);
        });
    } catch (e) { console.error("Error loading voices", e); }
}

function selectVoice(id, el) {
    selectedVoice = id;
    document.querySelectorAll('.spk-card').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
    
    if (id !== 'source') {
        const audio = new Audio("samples/" + id + ".mp3");
        audio.play().catch(() => console.warn("Preview not available"));
    }
}

window.startDubbing = async function() {
    const btn = document.getElementById('startBtn');
    const srtInput = document.getElementById('srtFile');
    
    if (!srtInput.files.length) {
        showToast("يرجى رفع ملف SRT", "#b91c1c");
        return;
    }

    btn.disabled = true;
    btn.innerText = "جاري الإرسال للسيرفر...";
    
    const srtText = await srtInput.files[0].text();
    
    // إصلاح الخطأ: إرسال رابط العينة الصوتية من جيتهب لكي يقلدها السيرفر
    const voiceUrl = selectedVoice === 'source' ? '' : `https://raw.githubusercontent.com/${GITHUB_USER}/${REPO_NAME}/main/samples/${selectedVoice}.mp3`;
    
    const payload = { 
        srt: srtText, 
        lang: selectedLang, 
        voice_mode: selectedVoice === 'source' ? 'source' : 'xtts', 
        voice_id: selectedVoice === 'source' ? '' : selectedVoice,
        voice_url: voiceUrl 
    };

    try {
        const res = await fetch(API_BASE + '/api/dub', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            credentials: 'include'
        });
        const data = await res.json();
        
        if (data.success) {
            currentJobId = data.job_id;
            document.getElementById('progressArea').style.display = 'block';
            document.getElementById('statusTxt').innerText = 'تم الاستلام! جاري المعالجة...';
            document.getElementById('progBar').style.width = '5%';
            pollInterval = setInterval(() => pollJob(currentJobId), 2000);
        } else { 
            showToast("خطأ: " + data.error, "#b91c1c"); 
            btn.disabled = false; 
            btn.innerText = "ابدأ معالجة الدبلجة";
        }
    } catch (e) { 
        showToast("فشل الاتصال بالسيرفر", "#b91c1c"); 
        btn.disabled = false; 
        btn.innerText = "ابدأ معالجة الدبلجة";
    }
};

async function pollJob(jobId) {
    try {
        const res = await fetch(API_BASE + '/api/job/' + jobId, { credentials: 'include' });
        const data = await res.json();
        
        if (data.status === 'processing') {
            document.getElementById('statusTxt').innerText = 'قيد المعالجة وتوليد الصوت...';
            const bar = document.getElementById('progBar');
            let cur = parseInt(bar.style.width) || 10;
            cur = Math.min(90, cur + 5);
            bar.style.width = cur + '%';
            document.getElementById('pctTxt').innerText = cur + '%';
            
        } else if (data.status === 'completed') {
            clearInterval(pollInterval);
            document.getElementById('statusTxt').innerText = 'اكتملت المعالجة!';
            document.getElementById('progBar').style.width = '100%';
            document.getElementById('pctTxt').innerText = '100%';
            
            document.getElementById('resCard').style.display = 'block';
            document.getElementById('dubAud').src = data.audio_url;
            document.getElementById('dlBtn').href = data.audio_url;
            
            document.getElementById('startBtn').disabled = false;
            document.getElementById('startBtn').innerText = "ابدأ معالجة الدبلجة";
            showToast("تمت الدبلجة بنجاح!", "#065f2c");
            checkAuth(); // تحديث الرصيد
            
        } else if (data.status === 'failed') {
            clearInterval(pollInterval);
            document.getElementById('statusTxt').innerText = 'فشلت المعالجة';
            showToast("فشلت عملية الدبلجة. تم استرجاع الرصيد.", "#b91c1c");
            document.getElementById('startBtn').disabled = false;
            document.getElementById('startBtn').innerText = "ابدأ معالجة الدبلجة";
            checkAuth();
        }
    } catch (e) { console.error("Polling error", e); }
}

async function checkAuth() {
    try {
        const res = await fetch(API_BASE + '/api/user', { credentials: 'include' });
        const data = await res.json();
        if (data.success) renderProfile(data.user);
    } catch (e) {}
}

function renderProfile(user) {
    const sec = document.getElementById('authSection');
    if (!sec) return;
    sec.innerHTML = `
    <div style="display:flex;gap:10px;align-items:center">
        <div style="text-align:right">
            <div style="font-weight:700">${user.name || 'مستخدم'}</div>
            <div style="background:rgba(255,255,255,0.06);padding:6px;border-radius:8px">رصيد: ${user.credits}</div>
        </div>
        <button class="auth-btn" onclick="location.reload()">خروج</button>
    </div>`;
}

function showToast(msg, color='#0f0f10') {
    const t = document.createElement('div');
    t.className = 'toast show';
    t.style.background = color;
    t.innerText = msg;
    const container = document.getElementById('toasts');
    if (container) { container.appendChild(t); setTimeout(()=>{ t.remove(); }, 3500); }
}
