const $ = (id) => document.getElementById(id);

const state = {
  lastOut: null,
  lastReport: null,
  busy: false,
};

function srcFmt() {
  return document.querySelector('input[name=srcfmt]:checked').value;
}
function dstFmt() {
  return document.querySelector('input[name=dst]:checked').value;
}

function log(text, kind) {
  const box = $('log');
  const div = document.createElement('div');
  if (kind) div.className = kind;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function setStatus(text, spinning) {
  $('status').innerHTML = (spinning ? '<span class="spin"></span> ' : '') + text;
}

$('ver').textContent = 'v1.0.0 · Electron ' + (window.nle ? window.nle.version() : '');

$('btnSrc').onclick = async () => {
  const p = srcFmt() === 'jianying'
    ? await window.nle.pickDir({ title: '选择剪映草稿目录（含 draft_content.json）' })
    : await window.nle.pickFile({ title: '选择交换文件' });
  if (p) $('src').value = p;
};

$('btnOut').onclick = async () => {
  const p = await window.nle.pickDir({ title: '选择输出目录' });
  if (p) $('out').value = p;
};

$('btnOpenOut').onclick = async () => {
  const p = $('out').value.trim() || state.lastOut;
  if (!p) { alert('还没有输出目录，先转换一次。'); return; }
  await window.nle.openPath(p);
};

$('btnReport').onclick = async () => {
  if (state.lastReport) await window.nle.openPath(state.lastReport);
};

$('btnClear').onclick = () => { $('log').innerHTML = ''; };

document.querySelectorAll('input[name=srcfmt]').forEach((el) => {
  el.onchange = () => { $('src').value = ''; };
});

function payload() {
  const src = $('src').value.trim();
  if (!src) { alert('先选一个源工程。'); return null; }
  const out = $('out').value.trim();
  if (!out) { alert('先选输出目录。'); return null; }
  return {
    src,
    src_fmt: srcFmt(),
    dst: dstFmt(),
    out,
    opt: {
      keep_watermark: $('optWm').checked,
      still_center: $('optStill').checked,
      y_down: $('optYDown').checked,
      scale_mode: $('optScale').value,
    },
  };
}

$('btnRun').onclick = async () => {
  if (state.busy) return;
  const req = payload();
  if (!req) return;

  state.busy = true;
  $('btnRun').disabled = true;
  $('btnReport').disabled = true;
  $('log').innerHTML = '';
  setStatus('正在转换…', true);

  const res = await window.nle.convert(req);

  state.busy = false;
  $('btnRun').disabled = false;
  if (res && res.ok) {
    state.lastOut = res.outdir || req.out;
    state.lastReport = res.report || null;
    $('btnReport').disabled = !state.lastReport;
    if (res.problems) {
      setStatus(`完成，${res.problems} 个待确认项（见日志 / 报告）`);
    } else {
      setStatus('完成，自检无问题');
    }
  } else {
    setStatus('转换失败');
    log('转换失败：' + ((res && res.error) || '未知错误'), 'err');
  }
};

window.nle.onLog((obj) => {
  if (obj.type === 'log') log(obj.text, obj.kind || undefined);
});

// 默认输出目录：桌面\nle-bridge-out
(async function initOut() {
  const p = await window.nle.defaultOut();
  if (p && !$('out').value) $('out').value = p;
})();
