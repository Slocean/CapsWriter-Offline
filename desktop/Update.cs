using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using System.Web.Script.Serialization;

// 程序内热更新：检查更新、显示中文更新公告、下载并应用当前形态的更新包。
// 版本唯一来源 installer/version.txt（编译进 AppVersion）；更新清单
// CapsWriter-Update-Manifest.json 由发布工作流随 GitHub Release 附带。
// 安全规则：清单 schema/版本/资产名/下载域名逐一校验；下载完成与替换前
// 各做一次 SHA256 校验；旧版本、错误资产、校验失败一律拒绝；替换失败
// 由便携版启动器自动回滚（见 installer/PortableLauncher.cs --replace-self）。
internal static partial class Desktop {
    const string UpdateManifestUrl =
        "https://github.com/Slocean/CapsWriter-Offline/releases/latest/download/CapsWriter-Update-Manifest.json";
    const string UpdateAssetUrlPrefix = "https://github.com/Slocean/CapsWriter-Offline/releases/download/";

    static TextBlock updateCurrent, updateStatus;
    static Button updateButton;
    static bool updateBusy;
    static DispatcherTimer updateAutoTimer;

    sealed class UpdateInfo {
        public string Version;
        public string Title;
        public string Announcement;
        public string AssetName;
        public string AssetUrl;
        public string AssetSha256;
        public long AssetSize;
    }

    static string UpdateUserAgent {
        get { return "CapsWriter-Desktop/" + AppVersion; }
    }

    // ---- 形态判定 -------------------------------------------------------

    static string OuterPortableExe() {
        string outer = Environment.GetEnvironmentVariable("CAPSWRITER_PORTABLE_EXE");
        if (string.IsNullOrEmpty(outer) || !File.Exists(outer)) return null;
        return outer;
    }

    // portable=便携单文件（可自替换）；portable-nohost=直接运行了解压缓存里的
    // GUI（没有外层 EXE 可替换，更新只能通过便携单文件入口进行）；setup=安装版。
    static string UpdateFlavor() {
        if (OuterPortableExe() != null) return "portable";
        string portableRoot = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "CapsWriterOffline", "Portable");
        if (Dir.StartsWith(portableRoot, StringComparison.OrdinalIgnoreCase)) return "portable-nohost";
        return "setup";
    }

    static string FlavorLabel() {
        string flavor = UpdateFlavor();
        return flavor == "portable" ? "便携版" : flavor == "portable-nohost" ? "便携版（内部副本）" : "安装版";
    }

    // ---- 界面 -----------------------------------------------------------

    // 返回 Disclosure 折叠区的内容（标题由 Disclosure 头提供）
    static UIElement UpdateSection() {
        var panel = new StackPanel();
        var row = new Grid { Margin = new Thickness(0, 2, 0, 0) };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var left = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
        updateCurrent = Text("当前版本 v" + AppVersion + "（" + FlavorLabel() + "）", 12, "#5A5D62", "#C7C9C8", true);
        left.Children.Add(updateCurrent);
        updateStatus = Text("通过 GitHub Releases 分发；更新前会校验 SHA256，失败自动回滚。", 11, "#7A7E83", "#A2A5A9");
        updateStatus.TextWrapping = TextWrapping.Wrap;
        updateStatus.Margin = new Thickness(0, 5, 12, 0);
        left.Children.Add(updateStatus);
        row.Children.Add(left);
        updateButton = ThemeButton("检查更新", "#26282B", "#EDEEEC", "#FFFFFF", "#1D1F22", 9);
        updateButton.Width = 96;
        updateButton.Height = 34;
        updateButton.FontSize = 11;
        updateButton.VerticalAlignment = VerticalAlignment.Center;
        updateButton.Click += (s, e) => CheckForUpdates(true);
        Grid.SetColumn(updateButton, 1);
        row.Children.Add(updateButton);
        panel.Children.Add(row);
        return panel;
    }

    static void SetUpdateStatus(string message) {
        if (updateStatus != null) updateStatus.Text = message;
    }

    static void SetUpdateBusy(bool busy) {
        updateBusy = busy;
        if (updateButton != null) {
            updateButton.Content = busy ? "检查中…" : "检查更新";
            updateButton.IsEnabled = !busy;
        }
    }

    // 启动后 6 秒做一次静默检查：发现新版本才弹公告，失败不打扰。
    static void ScheduleUpdateAutoCheck() {
        updateAutoTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(6) };
        updateAutoTimer.Tick += (s, e) => {
            updateAutoTimer.Stop();
            CheckForUpdates(false);
        };
        updateAutoTimer.Start();
    }

    static void CheckForUpdates(bool manual) {
        if (updateBusy) return;
        SetUpdateBusy(true);
        if (manual) SetUpdateStatus("正在检查更新…");
        ThreadPool.QueueUserWorkItem(delegate {
            UpdateInfo info = null;
            Exception error = null;
            try { info = FetchManifest(); }
            catch (Exception ex) { error = ex; }
            main.Dispatcher.BeginInvoke(new Action(delegate {
                if (error != null) {
                    SetUpdateBusy(false);
                    AppendLog("检查更新失败: " + error.Message);
                    if (manual) SetUpdateStatus("检查更新失败：" + ShortError(error.Message) + "（可再次点击重试）");
                    else SetUpdateStatus("检查更新暂时不可用，可手动点击「检查更新」。");
                    return;
                }
                if (CompareVersion(info.Version, AppVersion) <= 0) {
                    SetUpdateBusy(false);
                    SetUpdateStatus("已是最新版本 v" + AppVersion + "（" + DateTime.Now.ToString("HH:mm") + " 检查）");
                    return;
                }
                SetUpdateBusy(false);
                SetUpdateStatus("发现新版本 v" + info.Version + "，请在公告窗口中选择是否更新。");
                bool apply = ShowAnnouncement(info);
                if (apply) BeginUpdate(info);
            }));
        });
    }

    // ---- 公告窗口 -------------------------------------------------------

    static bool ShowAnnouncement(UpdateInfo info) {
        var window = new Window {
            Title = "CapsWriter 更新公告",
            Width = 560, Height = 560,
            WindowStartupLocation = WindowStartupLocation.CenterScreen,
            Background = T("#F6F7F5", "#141517"),
            FontFamily = main.FontFamily
        };
        var root = new Grid { Margin = new Thickness(24, 18, 24, 18) };
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var heading = new StackPanel();
        var titleRow = new TextBlock {
            FontSize = 19, FontWeight = FontWeights.SemiBold,
            Foreground = T("#1D1F22", "#F1F2F0"),
            Text = "发现新版本 v" + info.Version + "（当前 v" + AppVersion + " · " + FlavorLabel() + "）"
        };
        heading.Children.Add(titleRow);
        var subtitle = new TextBlock {
            FontSize = 13, Margin = new Thickness(0, 6, 0, 0),
            Foreground = T("#5A5D62", "#C7C9C8"), TextWrapping = TextWrapping.Wrap,
            Text = info.Title
        };
        heading.Children.Add(subtitle);
        Grid.SetRow(heading, 0);
        root.Children.Add(heading);

        var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Margin = new Thickness(0, 14, 0, 0) };
        var body = new TextBlock {
            FontSize = 13, TextWrapping = TextWrapping.Wrap,
            Foreground = T("#33363A", "#E7E8E6"),
            Text = info.Announcement
        };
        scroll.Content = body;
        Grid.SetRow(scroll, 1);
        root.Children.Add(scroll);

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 16, 0, 0) };
        var later = ThemeButton("以后再说", "#F0F1EE", "#2E3134", "#5A5D62", "#C7C9C8", 9);
        later.Width = 108; later.Height = 38;
        var now = ThemeButton("立即更新", "#26282B", "#EDEEEC", "#FFFFFF", "#1D1F22", 9);
        now.Width = 108; now.Height = 38; now.Margin = new Thickness(10, 0, 0, 0);
        bool apply = false;
        later.Click += (s, e) => { window.Close(); };
        now.Click += (s, e) => { apply = true; window.Close(); };
        buttons.Children.Add(later);
        buttons.Children.Add(now);
        Grid.SetRow(buttons, 2);
        root.Children.Add(buttons);

        window.Content = root;
        window.Owner = main;
        window.ShowDialog();
        return apply;
    }

    // ---- 下载与应用 -----------------------------------------------------

    static void BeginUpdate(UpdateInfo info) {
        SetUpdateBusy(true);
        SetUpdateStatus("正在下载 v" + info.Version + "…");
        ThreadPool.QueueUserWorkItem(delegate {
            string flavor = UpdateFlavor();
            Exception error = null;
            do {
                try {
                    if (flavor == "portable-nohost")
                        throw new InvalidOperationException("当前运行的是便携版解压后的内部副本。请通过便携版单文件 EXE 启动程序后再更新。");
                    // 下载到正式 .exe 文件名：CreateProcess / PowerShell 只认
                    // .exe 扩展名；暂存目录以"外层完整文件名"命名（与启动器
                    // CleanupUpdateArtifacts 的清理对象精确互相对应，不与其他
                    // 程序共享任何通配名），先清空旧内容。
                    string staging;
                    if (flavor == "portable") {
                        string outer = OuterPortableExe();
                        if (outer == null) throw new InvalidOperationException("找不到便携版外层 EXE。");
                        staging = Path.GetFullPath(outer) + ".update-staging";
                    } else {
                        staging = Path.Combine(Path.GetTempPath(), "CapsWriter-Update-" + info.Version);
                    }
                    if (Directory.Exists(staging)) { try { Directory.Delete(staging, true); } catch { } }
                    Directory.CreateDirectory(staging);
                    string downloadPath = Path.Combine(staging, info.AssetName);
                    DownloadAsset(info, downloadPath);
                    // 应用前重新计算磁盘文件的 SHA256（第二次校验）
                    string actual = Sha256File(downloadPath);
                    if (!string.Equals(actual, info.AssetSha256, StringComparison.OrdinalIgnoreCase)) {
                        TryDelete(downloadPath);
                        throw new InvalidDataException("更新包应用前校验失败，已取消更新。");
                    }
                    SetStatusThreadSafe("校验通过，正在应用更新…");
                    if (flavor == "portable") ApplyPortableUpdate(info, downloadPath);
                    else ApplySetupUpdate(info, downloadPath);
                    return; // Apply* 内部会退出进程
                } catch (Exception ex) { error = ex; }
            } while (false);
            main.Dispatcher.BeginInvoke(new Action(delegate {
                SetUpdateBusy(false);
                AppendLog("更新失败: " + error.Message);
                SetUpdateStatus("更新失败：" + ShortError(error.Message) + "（可重新点击「检查更新」重试）");
            }));
        });
    }

    // 更新退出闸门（UI 线程执行）：仅当 Exit() 确认（录音已结束、输出静音
    // 已恢复、后台客户端已终止）才允许 Application.Shutdown；未确认时取消
    // 本次更新并保持界面打开——绝不无条件关闭主界面。
    static bool ExitForUpdate() {
        if (!Exit()) {
            SetUpdateBusy(false);
            SetUpdateStatus("已取消更新：客户端退出握手未确认（录音或输出恢复未完成），界面保持打开。");
            AppendLog("更新已取消：客户端退出握手未确认，未执行更新。");
            return false;
        }
        Application.Current.Shutdown();
        return true;
    }

    // Exit() 关闭窗口/托盘并结束自己的后台客户端，必须在 UI 线程执行。
    // 返回 false = 退出握手未确认：不 Shutdown，界面保持打开。
    static bool ExitViaDispatcher() {
        return (bool)main.Dispatcher.Invoke(new Func<bool>(ExitForUpdate));
    }

    // 等待 wrapper 完成早期校验+备份并写入 READY 协议行（无时间戳前缀，
    // 且必须携带本次事务 id——旧结果文件里遗留的 READY 不可能被误认）。
    // 期间界面保持打开；wrapper 提前退出（无 READY）或超时都返回 false。
    static bool WaitForWrapperReady(Process wrapper, string resultPath, string txId, int timeoutMs) {
        int deadline = Environment.TickCount + timeoutMs;
        while (Environment.TickCount < deadline) {
            try { if (wrapper.HasExited) return false; } catch { return false; }
            try {
                if (File.Exists(resultPath)) {
                    foreach (string line in File.ReadAllLines(resultPath, Encoding.UTF8)) {
                        string trimmed = line.Trim();
                        if (!trimmed.StartsWith("READY", StringComparison.Ordinal)) continue;   // 带时间戳的普通日志行
                        string rest = trimmed.Length > 5 ? trimmed.Substring(5).Trim() : "";
                        if (txId.Length == 0 || rest == txId) return true;
                    }
                }
            } catch (IOException) { }   // wrapper 正在重写结果文件，读到半截忽略
            catch (UnauthorizedAccessException) { }
            Thread.Sleep(250);
        }
        return false;
    }

    // 在结果文件补写一条终态 RESULT 行（中止/取消时 wrapper 已被杀掉，不再
    // 有写入方），让结果文件在任何路径下都以终态收尾，重启后的 GUI 读取时
    // 不会看到"无终态"的半截日志。
    static void AppendLocalResultLine(string resultPath, string reason) {
        try {
            File.AppendAllText(resultPath,
                "RESULT: FAILED: " + reason + "\r\n", Encoding.UTF8);
        } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    // 更新中止（GUI 侧）：杀掉仍在等待 GUI 退出的 wrapper，恢复按钮可用，
    // 在界面与运行记录里给出诊断——不关闭界面。
    static void AbortUpdateOnUi(Process wrapper, string reason) {
        try { if (wrapper != null && !wrapper.HasExited) wrapper.Kill(); } catch { }
        main.Dispatcher.Invoke(new Action(delegate {
            SetUpdateBusy(false);
            SetUpdateStatus(reason);
            AppendLog(reason);
        }));
    }

    // 便携版：启动下载好的新单文件 EXE（--replace-self），由它在旧 GUI 退出后
    // 事务替换外层 EXE 并重启；随后本进程正常退出。替换失败会自动回滚。
    static void ApplyPortableUpdate(UpdateInfo info, string downloadPath) {
        string outer = OuterPortableExe();
        if (outer == null) throw new InvalidOperationException("找不到便携版外层 EXE。");
        SetStatusThreadSafe("正在等待界面退出并替换便携版…");
        int pid = Process.GetCurrentProcess().Id;
        var psi = new ProcessStartInfo(downloadPath) {
            Arguments = "--replace-self \"" + outer + "\" --wait-pid " + pid,
            UseShellExecute = false, CreateNoWindow = true
        };
        Process.Start(psi);
        // 握手未确认（Exit() false）时界面保持打开，启动器等待 PID 超时后
        // 自行放弃并回滚；确认后才 Shutdown。
        ExitViaDispatcher();
    }

    // 安装版：交给 update-setup-wrapper.ps1——wrapper 先做早期校验并备份
    // 当前安装目录，写入带本次事务 id 的 READY 后 GUI 才退出；随后静默安装
    // （显式 /DIR 指向当前安装目录）、校验、从原目录重启；所有失败路径恢复
    // 旧版、重启并写 update-setup-result.txt（RESULT 行，重启后的 GUI 会
    // 展示诊断）。
    // 根因修复（2.6.2/2.6.3 热更新"界面关闭后无响应"）：BaseDirectory 恒以
    // 反斜杠结尾，直接拼进引号参数时尾反斜杠转义闭合引号，CRT 把 -ResultPath
    // 吞进 -AppDir 单 token，wrapper 在参数绑定阶段就退出——这里把末尾反斜杠
    // 翻倍保证字面传递，并改用 System32 绝对路径启动 PowerShell（不受 PATH
    // 影响）。
    // 事务身份：-TxId 每次随机生成，wrapper 的 READY 行必须携带同一 id 才被
    // 视为本次事务的就绪信号。
    static void ApplySetupUpdate(UpdateInfo info, string downloadPath) {
        SetStatusThreadSafe("正在准备更新事务（校验脚本并备份当前安装）…");
        string wrapper = Path.Combine(Dir, "update-setup-wrapper.ps1");
        if (!File.Exists(wrapper))
            throw new InvalidOperationException("缺少更新辅助脚本 update-setup-wrapper.ps1。");
        string resultPath = Path.Combine(Dir, "update-setup-result.txt");
        try { File.Delete(resultPath); } catch { }
        int pid = Process.GetCurrentProcess().Id;
        string txId = Guid.NewGuid().ToString("N");
        string appDirArg = Dir.EndsWith("\\") ? Dir + "\\" : Dir;   // 尾反斜杠翻倍防转义闭合引号
        var psi = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory,
            "WindowsPowerShell", "v1.0", "powershell.exe")) {
            Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + wrapper + "\""
                + " -SetupPath \"" + downloadPath + "\""
                + " -GuiPid " + pid
                + " -AppDir \"" + appDirArg + "\""
                + " -ResultPath \"" + resultPath + "\""
                + " -TxId " + txId,
            UseShellExecute = false, CreateNoWindow = true
        };
        Process wrapperProc = Process.Start(psi);
        if (wrapperProc == null)
            throw new InvalidOperationException("无法启动 PowerShell 更新辅助进程。");
        // 界面保持打开，直到 wrapper 完成早期校验+备份并写入带本事务 id 的
        // READY；失败/超时都在这里中止并给出诊断，绝不无提示关闭界面。
        if (!WaitForWrapperReady(wrapperProc, resultPath, txId, 300000)) {
            bool killed = false;
            try { if (!wrapperProc.HasExited) { wrapperProc.Kill(); killed = true; } } catch { }
            if (killed) AppendLocalResultLine(resultPath,
                "wrapper did not reach READY in time; aborted by GUI; outcome: old install untouched");
            AbortUpdateOnUi(wrapperProc, "更新已中止：更新辅助脚本未完成准备（详见运行记录），界面保持打开。");
            return;
        }
        SetStatusThreadSafe("更新准备完成，正在退出界面并应用更新…");
        bool exited = (bool)main.Dispatcher.Invoke(new Func<bool>(ExitForUpdate));
        if (!exited) {
            // 退出握手未确认：取消更新，杀掉等待 GUI 退出的 wrapper（它的
            // 等待超时兜底会写入同样的失败终态），补写终态行，界面保持打开。
            try { if (!wrapperProc.HasExited) wrapperProc.Kill(); } catch { }
            AppendLocalResultLine(resultPath,
                "update cancelled before install (exit handshake unconfirmed; wrapper stopped); outcome: old install untouched");
        }
    }

    // 展示上一次更新事务的诊断（wrapper 恢复/重启 GUI 后，用户能看到结果）。
    // RESULT 行由 wrapper 在健康检查/恢复之后（重启 GUI 数秒前后）才写入；
    // 且结果文件可能正被写入方短暂独占——单次读取失败或单次"无终态"都不
    // 代表结束，在后台线程按轮询重试（每轮独立捕获共享冲突，不因一次
    // IOException 放弃），最多 20 秒；文件超过 30 分钟按过期处理。
    // 失败提示必须按 wrapper 写入的真实 outcome 区分（恢复成功/恢复未完成/
    // 旧版未受影响），绝不臆断"已恢复旧版本并重启"。
    static void CheckLastUpdateResult() {
        ThreadPool.QueueUserWorkItem(delegate {
            try {
                string path = Path.Combine(Dir, "update-setup-result.txt");
                string text = null;
                bool fresh = false;
                for (int attempt = 0; attempt < 41; attempt++) {
                    try {
                        var file = new FileInfo(path);
                        if (!file.Exists) { if (attempt > 0) break; }
                        else {
                            fresh = (DateTime.UtcNow - file.LastWriteTimeUtc).TotalMinutes <= 30;
                            if (!fresh) break;
                            text = File.ReadAllText(path, Encoding.UTF8);
                            if (text.IndexOf("RESULT:", StringComparison.Ordinal) >= 0) break;
                        }
                    } catch (IOException) { text = null; }   // 写入方占用中：本轮放弃，下一轮重试
                    catch (UnauthorizedAccessException) { text = null; }
                    Thread.Sleep(500);
                }
                if (text == null || !fresh) return;
                string resultLine = null;
                foreach (string raw in text.Split('\n')) {
                    string trimmed = raw.Trim();
                    if (trimmed.StartsWith("RESULT:", StringComparison.Ordinal)) resultLine = trimmed;
                }
                if (resultLine == null) {
                    main.Dispatcher.BeginInvoke(new Action(delegate {
                        AppendLog("上次更新事务日志（无终态结果行，事务可能被中断）：" + Environment.NewLine + text);
                        SetUpdateStatus("上次更新事务未写入终态结果（可能被中断）；详见运行记录。");
                    }));
                    return;
                }
                bool ok = resultLine.StartsWith("RESULT: OK", StringComparison.Ordinal);
                string combined = resultLine;
                main.Dispatcher.BeginInvoke(new Action(delegate {
                    AppendLog("上次更新事务日志：" + Environment.NewLine + text);
                    if (ok) {
                        SetUpdateStatus("上次更新已完成，程序已从新版本重启。");
                        return;
                    }
                    // 失败原因 + wrapper 恢复结果（旧版 wrapper 无 outcome 段时按原文提示）
                    string status;
                    if (combined.Contains("recovery incomplete"))
                        status = "上次更新失败：恢复未完成，安装目录旁保留了 .update-backup 备份，需要手动处理；详见运行记录。";
                    else if (combined.Contains("old version restored and restarted"))
                        status = "上次更新失败：已恢复旧版本并重启；详见运行记录。";
                    else if (combined.Contains("old install untouched"))
                        status = "上次更新失败：旧版本未受影响，安装文件未改动；详见运行记录。";
                    else
                        status = "上次更新失败：" + ShortError(combined.Substring(7)) + "；详见运行记录。";
                    SetUpdateStatus(status);
                }));
            } catch (IOException) { } catch (UnauthorizedAccessException) { }
        });
    }

    static void SetStatusThreadSafe(string message) {
        main.Dispatcher.BeginInvoke(new Action(delegate { SetUpdateStatus(message); }));
    }

    static void TryDelete(string path) {
        try { if (File.Exists(path)) File.Delete(path); } catch { }
    }

    // ---- 清单获取与校验 --------------------------------------------------

    static UpdateInfo FetchManifest() {
        string json = HttpGetString(UpdateManifestUrl, 20000);
        var serializer = new JavaScriptSerializer { MaxJsonLength = 1 << 22 };
        var root = serializer.Deserialize<Dictionary<string, object>>(json);
        Require(root != null, "更新清单不是 JSON 对象");
        Require(Convert.ToInt32(root["schema"]) == 1, "更新清单版本不支持");
        string latest = AsString(root, "latest");
        Require(Regex.IsMatch(latest, @"^\d+\.\d+\.\d+$"), "更新清单版本号无效");
        string title = AsString(root, "title");
        Require(!string.IsNullOrWhiteSpace(title), "更新清单缺少标题");
        string announcement = AsString(root, "announcement");
        Require(!string.IsNullOrWhiteSpace(announcement), "更新清单缺少更新公告");

        var assets = root["assets"] as Dictionary<string, object>;
        Require(assets != null, "更新清单缺少更新包信息");
        string flavor = UpdateFlavor() == "setup" ? "setup" : "portable";
        var asset = assets[flavor] as Dictionary<string, object>;
        Require(asset != null, "更新清单缺少当前形态（" + flavor + "）的更新包");
        string expectedName = (flavor == "setup" ? "CapsWriter-Setup-" : "CapsWriter-Portable-") + latest + ".exe";
        string name = AsString(asset, "name");
        Require(name == expectedName, "更新包名称与版本不符：" + name);
        string url = AsString(asset, "url");
        Require(url.StartsWith(UpdateAssetUrlPrefix, StringComparison.OrdinalIgnoreCase),
            "更新包下载地址不是官方 GitHub Releases");
        string sha256 = AsString(asset, "sha256");
        Require(Regex.IsMatch(sha256, @"^[0-9a-f]{64}$"), "更新包 SHA256 无效");
        long size = Convert.ToInt64(asset["size"]);
        Require(size > 0, "更新包大小无效");

        return new UpdateInfo {
            Version = latest, Title = title, Announcement = announcement,
            AssetName = name, AssetUrl = url, AssetSha256 = sha256, AssetSize = size
        };
    }

    static string AsString(Dictionary<string, object> map, string key) {
        if (map == null || !map.ContainsKey(key) || map[key] == null) return "";
        return Convert.ToString(map[key]);
    }

    static void Require(bool condition, string message) {
        if (!condition) throw new InvalidDataException(message);
    }

    static string ShortError(string message) {
        if (string.IsNullOrEmpty(message)) return "未知错误";
        int newline = message.IndexOfAny(new[] { '\r', '\n' });
        string line = newline >= 0 ? message.Substring(0, newline) : message;
        return line.Length > 160 ? line.Substring(0, 160) + "…" : line;
    }

    // ---- 版本比较 -------------------------------------------------------

    // -1：a 旧于 b；0：相同；1：a 新于 b。旧版本/相同版本都会被拒绝更新。
    static int CompareVersion(string a, string b) {
        long[] left = VersionTriple(a), right = VersionTriple(b);
        for (int i = 0; i < 3; i++) {
            if (left[i] != right[i]) return left[i] < right[i] ? -1 : 1;
        }
        return 0;
    }

    static long[] VersionTriple(string version) {
        var result = new long[3];
        if (string.IsNullOrEmpty(version)) return result;
        string[] parts = version.Split('.');
        for (int i = 0; i < 3 && i < parts.Length; i++) {
            long value;
            if (long.TryParse(parts[i], out value)) result[i] = value;
        }
        return result;
    }

    // ---- 网络与哈希 -----------------------------------------------------

    static void PrepareTls() {
        try { ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072; } catch { }
        ServicePointManager.DefaultConnectionLimit = Math.Max(ServicePointManager.DefaultConnectionLimit, 4);
    }

    static HttpWebRequest UpdateRequest(string url, int timeout) {
        PrepareTls();
        var request = (HttpWebRequest)WebRequest.Create(url);
        request.UserAgent = UpdateUserAgent;
        request.Timeout = timeout;
        request.ReadWriteTimeout = timeout;
        request.AllowAutoRedirect = true;
        return request;
    }

    static string HttpGetString(string url, int timeout) {
        using (var response = (HttpWebResponse)UpdateRequest(url, timeout).GetResponse())
        using (var stream = response.GetResponseStream())
        using (var reader = new StreamReader(stream, Encoding.UTF8)) {
            return reader.ReadToEnd();
        }
    }

    static string Sha256File(string path) {
        using (var stream = File.OpenRead(path))
        using (var sha = SHA256.Create()) {
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }
    }

    // 下载到临时文件，边下载边计算 SHA256（下载时校验）；不一致立即删除。
    static void DownloadAsset(UpdateInfo info, string downloadPath) {
        string parent = Path.GetDirectoryName(Path.GetFullPath(downloadPath));
        if (!Directory.Exists(parent)) Directory.CreateDirectory(parent);
        TryDelete(downloadPath);
        using (var response = (HttpWebResponse)UpdateRequest(info.AssetUrl, 60000).GetResponse())
        using (var input = response.GetResponseStream())
        using (var output = File.Create(downloadPath))
        using (var sha = SHA256.Create()) {
            byte[] buffer = new byte[65536];
            long total = 0, reported = 0;
            while (true) {
                int read = input.Read(buffer, 0, buffer.Length);
                if (read <= 0) break;
                output.Write(buffer, 0, read);
                sha.TransformBlock(buffer, 0, read, null, 0);
                total += read;
                if (total - reported >= 2L << 20) {
                    reported = total;
                    SetStatusThreadSafe("正在下载 v" + info.Version + "… " + (total >> 20) + " MB");
                }
            }
            sha.TransformFinalBlock(buffer, 0, 0);
            string hash = BitConverter.ToString(sha.Hash).Replace("-", "").ToLowerInvariant();
            output.Flush();
            if (!string.Equals(hash, info.AssetSha256, StringComparison.OrdinalIgnoreCase)) {
                TryDelete(downloadPath);
                throw new InvalidDataException("更新包下载校验失败（SHA256 不符），已删除下载文件。");
            }
        }
    }
}
