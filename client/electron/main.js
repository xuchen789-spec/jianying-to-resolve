// NLE Bridge 客户端主进程
const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const APP_VER = '1.0.0';

// 这个界面不需要 GPU 加速；关掉能避开一堆虚拟机/远程桌面下的渲染崩溃
app.disableHardwareAcceleration();
let win = null;
let running = false;

function pythonPath() {
  // 打包后: <安装目录>/resources/python/python.exe ；开发时用环境变量覆盖
  const bundled = path.join(process.resourcesPath, 'python', 'python.exe');
  if (fs.existsSync(bundled)) return bundled;
  return process.env.NLE_PYTHON || 'python';
}

function createWindow() {
  win = new BrowserWindow({
    width: 1120,
    height: 780,
    minWidth: 920,
    minHeight: 600,
    backgroundColor: '#14161a',
    title: `NLE Bridge ${APP_VER}`,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, 'index.html'));
  win.on('closed', () => { win = null; });
}

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
app.on('activate', () => { if (!win) createWindow(); });

ipcMain.handle('pick-dir', async (_e, opts) => {
  const r = await dialog.showOpenDialog(win, {
    title: (opts && opts.title) || '选择目录',
    properties: ['openDirectory'],
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle('pick-file', async (_e, opts) => {
  const r = await dialog.showOpenDialog(win, {
    title: (opts && opts.title) || '选择文件',
    properties: ['openFile'],
    filters: [
      { name: '交换文件', extensions: ['fcpxml', 'xml', 'json'] },
      { name: '所有文件', extensions: ['*'] },
    ],
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle('default-out', () => {
  const desktop = app.getPath('desktop');
  return path.join(desktop, 'nle-bridge-out');
});

ipcMain.handle('open-path', async (_e, p) => {
  if (!p) return false;
  try {
    if (fs.existsSync(p)) await shell.openPath(p);
    return true;
  } catch (e) { return false; }
});

ipcMain.handle('convert', async (_e, req) => {
  if (running) return { ok: false, error: '已有任务在跑' };
  running = true;
  return new Promise((resolve) => {
    const py = pythonPath();
    const worker = path.join(__dirname, 'worker.py');
    const child = spawn(py, [worker], {
      cwd: __dirname,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    });

    let buf = '';
    let final = null;
    const send = (obj) => { if (win && !win.isDestroyed()) win.webContents.send('log', obj); };

    child.stdout.on('data', (d) => {
      buf += d.toString('utf8');
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (!line) continue;
        let obj;
        try { obj = JSON.parse(line); } catch (e) { send({ type: 'log', text: line }); continue; }
        if (obj.type === 'done') final = obj;
        else send(obj);
      }
    });
    child.stderr.on('data', (d) => send({ type: 'log', text: d.toString('utf8').trimEnd(), kind: 'err' }));
    child.on('close', (code) => {
      running = false;
      if (!final) final = { type: 'done', ok: false, error: `后台进程退出（代码 ${code}）` };
      resolve(final);
    });
    child.on('error', (err) => {
      running = false;
      resolve({ type: 'done', ok: false, error: String(err) });
    });

    child.stdin.write(JSON.stringify(req));
    child.stdin.end();
  });
});
