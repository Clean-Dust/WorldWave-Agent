/* core/mascot/ww_mascot_overlay.cs — Fat Shark transparent desktop overlay
 * C# 5 compatible - blink, sit, lie, awake with click interaction
 */
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Windows.Forms;

public class MascotOverlay : Form
{
    // ── Win32 ──
    const int WS_EX_LAYERED = 0x80000;
    const int WS_EX_TOPMOST = 0x8;
    const int WS_EX_TOOLWINDOW = 0x80;
    const int GWL_EXSTYLE = -20;
    const int LWA_ALPHA = 0x2;

    [DllImport("user32.dll")]
    static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);
    [DllImport("user32.dll")]
    static extern int GetWindowLong(IntPtr hWnd, int nIndex);
    [DllImport("user32.dll")]
    static extern bool UpdateLayeredWindow(IntPtr hwnd, IntPtr hdcDst,
        ref Point pptDst, ref Size psize, IntPtr hdcSrc, ref Point pptSrc,
        int crKey, ref BlendFunction pblend, int dwFlags);
    [DllImport("gdi32.dll")]
    static extern IntPtr CreateCompatibleDC(IntPtr hdc);
    [DllImport("gdi32.dll")]
    static extern bool DeleteDC(IntPtr hdc);
    [DllImport("gdi32.dll")]
    static extern IntPtr SelectObject(IntPtr hdc, IntPtr hgdiobj);
    [DllImport("gdi32.dll")]
    static extern bool DeleteObject(IntPtr hgdiobj);
    [DllImport("user32.dll")]
    static extern IntPtr GetDC(IntPtr hWnd);
    [DllImport("user32.dll")]
    static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
    [DllImport("user32.dll")]
    static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);
    [DllImport("user32.dll")]
    static extern bool UnregisterHotKey(IntPtr hWnd, int id);

    [StructLayout(LayoutKind.Sequential)]
    struct BlendFunction { public byte BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat; }
    const byte AC_SRC_OVER = 0x00, AC_SRC_ALPHA = 0x01;

    const int HOTKEY_TOGGLE = 1, HOTKEY_KILL = 2;
    const uint MOD_CTRL = 0x0002, MOD_ALT = 0x0001;
    const uint VK_M = 0x4D, VK_K = 0x4B;

    // ── Sizing ──
    const int S = 66;
    int _x, _y;

    // ── Core state ──
    string _baseState = "idle"; // The "real" state from server
    string _displayState = "idle"; // What we're actually showing (might be blink, idle, etc.)
    string _pngDir;
    Bitmap _currentImage;
    NotifyIcon _trayIcon;
    bool _visible = true;

    // ── Timers ──
    System.Threading.Timer _pollTimer;
    System.Threading.Timer _blinkTimer;
    System.Threading.Timer _idleTimer;
    DateTime _lastActivity = DateTime.Now;

    // ── Drag ──
    bool _dragging = false;
    int _dragOffX, _dragOffY;

    // ── Idle progression ──
    enum FatigueLevel { Awake, Sitting, Lying }
    FatigueLevel _fatigue = FatigueLevel.Awake;

    public MascotOverlay()
    {
        _pngDir = @"C:\Users\Public\playwright\mascot_states";
        var screen = Screen.PrimaryScreen.WorkingArea;
        _x = screen.Right - S - 8;
        _y = screen.Bottom - S - 8;

        Text = "WW Mascot";
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        Location = new Point(_x, _y);
        Size = new Size(S, S);

        this.Load += OnLoad;
        this.MouseDown += OnMouseDown;
        this.MouseMove += OnMouseMove;
        this.MouseUp += OnMouseUp;
        this.Click += OnClick; // Also handle click for wake-up

        // System tray
        _trayIcon = new NotifyIcon();
        _trayIcon.Text = "WW Mascot (Fat Shark)";
        _trayIcon.Icon = SystemIcons.Application;
        _trayIcon.Visible = true;
        var m = new ContextMenu();
        m.MenuItems.Add("Toggle (Ctrl+Alt+M)", (s, e) => Toggle());
        m.MenuItems.Add("Quit (Ctrl+Alt+K)", (s, e) => { _trayIcon.Visible = false; Application.Exit(); });
        _trayIcon.ContextMenu = m;
        _trayIcon.DoubleClick += (s, e) => Toggle();
    }

    void OnLoad(object sender, EventArgs e)
    {
        int ex = GetWindowLong(Handle, GWL_EXSTYLE);
        SetWindowLong(Handle, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW);

        _currentImage = LoadStatePng("idle");
        RedrawLayered();

        RegisterHotKey(Handle, HOTKEY_TOGGLE, MOD_CTRL | MOD_ALT, VK_M);
        RegisterHotKey(Handle, HOTKEY_KILL, MOD_CTRL | MOD_ALT, VK_K);

        // Poll WW server
        _pollTimer = new System.Threading.Timer(PollState, null, 1000, 2000);

        // Blink every 3-5 seconds
        _blinkTimer = new System.Threading.Timer(DoBlink, null, 3000, 3500);

        // Idle fatigue: check every 10 seconds
        _idleTimer = new System.Threading.Timer(CheckFatigue, null, 10000, 10000);
    }

    // ── Blink ──
    void DoBlink(object state)
    {
        if (_fatigue != FatigueLevel.Awake) return; // Don't blink when sitting/lying
        try
        {
            SetDisplay("blink", 150); // 150ms
        }
        catch { }
    }

    // ── Idle fatigue ──
    void CheckFatigue(object state)
    {
        try
        {
            var elapsed = DateTime.Now - _lastActivity;
            FatigueLevel newLevel;

            if (elapsed.TotalSeconds < 30)
                newLevel = FatigueLevel.Awake;
            else if (elapsed.TotalSeconds < 60)
                newLevel = FatigueLevel.Sitting;
            else
                newLevel = FatigueLevel.Lying;

            if (newLevel != _fatigue)
            {
                _fatigue = newLevel;
                string stateName = _baseState;
                if (_fatigue == FatigueLevel.Sitting && _baseState == "idle")
                    stateName = "sitting";
                else if (_fatigue == FatigueLevel.Lying && _baseState == "idle")
                    stateName = "lying";
                SetDisplay(stateName, -1);
            }
        }
        catch { }
    }

    // ── Click to wake ──
    void OnClick(object sender, EventArgs e)
    {
        WakeUp();
    }

    void WakeUp()
    {
        if (_fatigue == FatigueLevel.Awake) return;

        _fatigue = FatigueLevel.Awake;
        _lastActivity = DateTime.Now;

        // Show "awake" for 600ms then back to base
        SetDisplay("awake", 600);
    }

    // ── Helper: set display with optional auto-revert ──
    void SetDisplay(string stateName, int revertMs)
    {
        var old = _currentImage;
        _currentImage = LoadStatePng(stateName);
        _displayState = stateName;
        BeginInvoke(new Action(RedrawLayered));
        if (old != null) old.Dispose();

        if (revertMs > 0)
        {
            // Auto-revert after timeout (using a fresh timer callback)
            var t = new System.Threading.Timer(_ =>
            {
                string target = _baseState;
                if (target == "idle" && _fatigue == FatigueLevel.Sitting) target = "sitting";
                else if (target == "idle" && _fatigue == FatigueLevel.Lying) target = "lying";

                if (_displayState != target)
                {
                    var old2 = _currentImage;
                    _currentImage = LoadStatePng(target);
                    _displayState = target;
                    BeginInvoke(new Action(RedrawLayered));
                    if (old2 != null) old2.Dispose();
                }
            }, null, revertMs, System.Threading.Timeout.Infinite);
        }
    }

    // ── Drag ──
    void OnMouseDown(object sender, MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Left)
        {
            _dragging = true;
            _dragOffX = e.X;
            _dragOffY = e.Y;
        }
    }

    void OnMouseMove(object sender, MouseEventArgs e)
    {
        if (_dragging)
        {
            _x = Left + e.X - _dragOffX;
            _y = Top + e.Y - _dragOffY;
            RedrawLayered();
        }
    }

    void OnMouseUp(object sender, MouseEventArgs e)
    {
        if (_dragging)
        {
            _dragging = false;
            _lastActivity = DateTime.Now; // Reset idle timer on drag
        }
    }

    // ── Hotkeys ──
    protected override void WndProc(ref Message m)
    {
        if (m.Msg == 0x0312) // WM_HOTKEY
        {
            int id = m.WParam.ToInt32();
            if (id == HOTKEY_TOGGLE) Toggle();
            else if (id == HOTKEY_KILL) { _trayIcon.Visible = false; Application.Exit(); }
            return;
        }
        base.WndProc(ref m);
    }

    void Toggle()
    {
        _visible = !_visible;
        if (_visible) { Show(); RedrawLayered(); } else Hide();
    }

    // ── Layered render ──
    void RedrawLayered()
    {
        if (_currentImage == null) return;
        Location = new Point(_x, _y);

        IntPtr screenDc = GetDC(IntPtr.Zero);
        IntPtr memDc = CreateCompatibleDC(screenDc);
        IntPtr hBitmap = _currentImage.GetHbitmap(Color.FromArgb(0));
        IntPtr oldBitmap = SelectObject(memDc, hBitmap);

        var dst = new Point(_x, _y);
        var sz = new Size(_currentImage.Width, _currentImage.Height);
        var src = new Point(0, 0);
        var blend = new BlendFunction();
        blend.BlendOp = AC_SRC_OVER;
        blend.SourceConstantAlpha = 255;
        blend.AlphaFormat = AC_SRC_ALPHA;
        UpdateLayeredWindow(Handle, screenDc, ref dst, ref sz, memDc, ref src, 0, ref blend, LWA_ALPHA);

        SelectObject(memDc, oldBitmap);
        DeleteObject(hBitmap);
        DeleteDC(memDc);
        ReleaseDC(IntPtr.Zero, screenDc);
    }

    Bitmap LoadStatePng(string state)
    {
        var path = Path.Combine(_pngDir, state + ".png");
        if (File.Exists(path))
        {
            using (var src = new Bitmap(path))
            {
                var bmp = new Bitmap(S, (int)((float)S / src.Width * src.Height), PixelFormat.Format32bppArgb);
                using (var g = Graphics.FromImage(bmp))
                {
                    g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    g.DrawImage(src, 0, 0, bmp.Width, bmp.Height);
                }
                return bmp;
            }
        }
        return null;
    }

    void PollState(object state)
    {
        try
        {
            string ns = "idle";
            try
            {
                var wc = new WebClient();
                wc.Headers["User-Agent"] = "WW-Mascot/1.0";
                string json = wc.DownloadString("http://localhost:9300/ww/mascot/state");
                var m = Regex.Match(json, "\"state\"\\s*:\\s*\"([^\"]+)\"");
                if (m.Success) ns = m.Groups[1].Value;
            }
            catch { }

            if (ns != _baseState)
            {
                _baseState = ns;
                // Don't override sitting/lying display states
                if (_fatigue == FatigueLevel.Awake || (_fatigue != FatigueLevel.Awake && ns != "idle"))
                {
                    var old = _currentImage;
                    _currentImage = LoadStatePng(ns);
                    _displayState = ns;
                    if (_visible)
                        BeginInvoke(new Action(RedrawLayered));
                    if (old != null) old.Dispose();
                }
            }
        }
        catch { }
    }

    [STAThread]
    public static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new MascotOverlay());
    }
}
