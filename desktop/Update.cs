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

    static UIElement UpdateSection() {
        var panel = new StackPanel();
        panel.Children.Add(Text("软件更新", 13, "#333538", "#E6E7E6", true));
        var row = new Grid { Margin = new Thickness(0, 12, 0, 0) };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var left = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
        updateCurrent = Text("当前版本 v" + AppVersion + "（" + FlavorLabel() + "）", 12, "#545659", "#C4C6C7", true);
        left.Children.Add(updateCurrent);
        updateStatus = Text("通过 GitHub Releases 分发；更新前会校验 SHA256，失败自动回滚。", 11, "#77797C", "#A4A6A8");
        updateStatus.TextWrapping = TextWrapping.Wrap;
        updateStatus.Margin = new Thickness(0, 5, 12, 0);
        left.Children.Add(updateStatus);
        row.Children.Add(left);
        updateButton = ThemeButton("检查更新", "#222326", "#F0F0EE", "#FFFFFF", "#1C1D1E", 9);
        updateButton.Width = 96;
        updateButton.Height = 34;
        updateButton.FontSize = 11;
        updateButton.VerticalAlignment = VerticalAlignment.Center;
        updateButton.Click += (s, e) => CheckForUpdates(true);
        Grid.SetColumn(updateButton, 1);
        row.Children.Add(updateButton);
        panel.Children.Add(row);
        return Surface(panel, 19);
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
            Background = T("#F7F7F5", "#121315"),
            FontFamily = main.FontFamily
        };
        var root = new Grid { Margin = new Thickness(24, 18, 24, 18) };
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var heading = new StackPanel();
        var titleRow = new TextBlock {
            FontSize = 19, FontWeight = FontWeights.SemiBold,
            Foreground = T("#1B1C1F", "#F3F3F1"),
            Text = "发现新版本 v" + info.Version + "（当前 v" + AppVersion + " · " + FlavorLabel() + "）"
        };
        heading.Children.Add(titleRow);
        var subtitle = new TextBlock {
            FontSize = 13, Margin = new Thickness(0, 6, 0, 0),
            Foreground = T("#545659", "#C4C6C7"), TextWrapping = TextWrapping.Wrap,
            Text = info.Title
        };
        heading.Children.Add(subtitle);
        Grid.SetRow(heading, 0);
        root.Children.Add(heading);

        var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Margin = new Thickness(0, 14, 0, 0) };
        var body = new TextBlock {
            FontSize = 13, TextWrapping = TextWrapping.Wrap,
            Foreground = T("#333538", "#D8DAD9"),
            Text = info.Announcement
        };
        scroll.Content = body;
        Grid.SetRow(scroll, 1);
        root.Children.Add(scroll);

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 16, 0, 0) };
        var later = ThemeButton("以后再说", "#F1F2F0", "#303235", "#4A4C4F", "#D1D3D3", 9);
        later.Width = 108; later.Height = 38;
        var now = ThemeButton("立即更新", "#222326", "#F0F0EE", "#FFFFFF", "#1C1D1E", 9);
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
                    string downloadPath;
                    if (flavor == "portable") {
                        string outer = OuterPortableExe();
                        if (outer == null) throw new InvalidOperationException("找不到便携版外层 EXE。");
                        downloadPath = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(outer)), info.AssetName + ".update-download");
                    } else {
                        downloadPath = Path.Combine(Path.GetTempPath(), info.AssetName + ".update-download");
                    }
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

    // Exit() 关闭窗口/托盘并结束自己的后台客户端，必须在 UI 线程执行。
    static void ExitViaDispatcher() {
        main.Dispatcher.Invoke(new Action(delegate {
            Exit();
            Application.Current.Shutdown();
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
        ExitViaDispatcher();
    }

    // 安装版：用 PowerShell 等待本进程退出后静默运行新安装包；Inno 沿用
    // 上次安装目录（UsePreviousAppDir），配置/热词/凭据保留。
    static void ApplySetupUpdate(UpdateInfo info, string downloadPath) {
        SetStatusThreadSafe("正在等待界面退出并启动安装程序…");
        int pid = Process.GetCurrentProcess().Id;
        string script = "try { Wait-Process -Id " + pid + " -Timeout 60 -ErrorAction Stop } catch { }\r\n" +
            "& '" + downloadPath.Replace("'", "''") + "' /VERYSILENT /SUPPRESSMSGBOXES /NORESTART";
        string encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(script));
        var psi = new ProcessStartInfo("powershell.exe") {
            Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand " + encoded,
            UseShellExecute = false, CreateNoWindow = true
        };
        Process.Start(psi);
        ExitViaDispatcher();
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
