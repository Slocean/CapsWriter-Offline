using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;
using System.Windows.Forms;

// 便携版单文件启动器：运行时把内嵌 payload 解压到
// %LOCALAPPDATA%\CapsWriterOffline\Portable\<version>-<hash16> 后启动 GUI。
// 版本号与 payload 哈希由 installer/build_portable.py 在编译期注入。
// 热更新：GUI 下载并校验新单文件 EXE 后，以 --replace-self 调用新 EXE，
// 由它等待旧 GUI 退出、事务替换外层 EXE 并重启；失败自动回滚。
internal static class PortableLauncher {
    const string Version = "__VERSION__";
    const string PayloadHash = "__PAYLOAD_HASH__";

    static string SelfExe {
        get {
            try { return Assembly.GetExecutingAssembly().Location; }
            catch { return Process.GetCurrentProcess().MainModule.FileName; }
        }
    }

    // 清理上次热更新残留（下载临时文件、旧版备份）；文件仍被占用时忽略，下次启动再清。
    static void CleanupUpdateArtifacts() {
        try {
            string self = SelfExe;
            if (string.IsNullOrEmpty(self)) return;
            string dir = Path.GetDirectoryName(Path.GetFullPath(self));
            if (dir == null || !Directory.Exists(dir)) return;
            string stem = Path.GetFileNameWithoutExtension(self);
            foreach (string pattern in new[] { stem + ".update-*", stem + ".update-bak" }) {
                foreach (string file in Directory.GetFiles(dir, pattern)) {
                    try { File.Delete(file); } catch { }
                }
            }
        } catch { }
    }

    static string Extract(string root) {
        root = Path.GetFullPath(root);
        Directory.CreateDirectory(root);
        string cache = Path.Combine(root, Version + "-" + PayloadHash.Substring(0, 16));
        using (var mutex = new Mutex(false, "Local\\CapsWriterPortable-" + PayloadHash.Substring(0, 16))) {
            bool acquired = false;
            try {
                try { acquired = mutex.WaitOne(TimeSpan.FromSeconds(60)); }
                catch (AbandonedMutexException) { acquired = true; }
                if (!acquired) throw new IOException("另一个便携版正在准备文件，请稍后再试。");
                string marker = Path.Combine(cache, ".payload-ready");
                if (File.Exists(marker) && File.ReadAllText(marker) == PayloadHash &&
                    File.Exists(Path.Combine(cache, "CapsWriterDesktop.exe")) &&
                    File.Exists(Path.Combine(cache, "start_client.exe")) &&
                    File.Exists(Path.Combine(cache, "config_client.py"))) return cache;
                if (Directory.Exists(cache)) throw new IOException("便携版缓存不完整：" + cache);
                string staging = Path.Combine(root, ".unpack-" + Guid.NewGuid().ToString("N"));
                Directory.CreateDirectory(staging);
                using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("CapsWriter.Payload.zip")) {
                    if (stream == null) throw new InvalidDataException("便携版数据不存在。");
                    using (var sha = SHA256.Create()) {
                        string hash = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
                        if (hash != PayloadHash) throw new InvalidDataException("便携版文件校验失败。");
                    }
                    stream.Position = 0;
                    using (var zip = new ZipArchive(stream, ZipArchiveMode.Read)) {
                        foreach (var entry in zip.Entries) {
                            string dest = Path.GetFullPath(Path.Combine(staging, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                            if (!dest.StartsWith(staging + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                                throw new InvalidDataException("便携版中存在非法文件路径。");
                            if (entry.FullName.EndsWith("/")) { Directory.CreateDirectory(dest); continue; }
                            Directory.CreateDirectory(Path.GetDirectoryName(dest));
                            using (var input = entry.Open()) using (var output = File.Create(dest)) input.CopyTo(output);
                        }
                    }
                }
                // 迁移上一个版本的设置：配置、热词、界面偏好。旧缓存目录整体保留，
                // 里面的日志与历史录音不会丢失。
                string previous = null;
                foreach (string directory in Directory.GetDirectories(root)) {
                    if (directory == staging || !File.Exists(Path.Combine(directory, ".payload-ready"))) continue;
                    if (previous == null || Directory.GetLastWriteTimeUtc(directory) > Directory.GetLastWriteTimeUtc(previous)) previous = directory;
                }
                if (previous != null) {
                    foreach (string file in new[] { "config_client.py", "hot.txt", "hot-server.txt", "hot-rule.txt", "desktop_ui.ini" }) {
                        string source = Path.Combine(previous, file);
                        if (File.Exists(source)) File.Copy(source, Path.Combine(staging, file), true);
                    }
                }
                File.WriteAllText(Path.Combine(staging, ".payload-ready"), PayloadHash);
                Directory.Move(staging, cache);
                return cache;
            } finally { if (acquired) mutex.ReleaseMutex(); }
        }
    }

    static int WaitForExit(int pid, int timeoutSeconds) {
        try {
            using (var process = Process.GetProcessById(pid)) {
                return process.WaitForExit(timeoutSeconds * 1000) ? 0 : 2;
            }
        } catch (ArgumentException) { return 0; } // 进程已不存在
        catch (Exception) { return 2; }
    }

    // --replace-self <旧EXE路径> [--wait-pid <pid>]：
    // 等待旧 GUI 退出 → 旧 EXE 改名备份 → 用自己覆盖旧 EXE → 校验 → 启动 → 成功后退出。
    // 任何一步失败都把备份改回原名并报错，绝不留下损坏的外层 EXE。
    static int ReplaceSelf(string target, int waitPid) {
        try {
            target = Path.GetFullPath(target);
            if (!File.Exists(target)) throw new FileNotFoundException("找不到要替换的便携版 EXE。", target);
            if (waitPid > 0 && WaitForExit(waitPid, 120) != 0)
                throw new IOException("等待旧界面退出超时，已取消更新。");
            string self = Path.GetFullPath(SelfExe);
            string backup = target + ".update-bak";
            if (File.Exists(backup)) { try { File.Delete(backup); } catch { } }
            File.Move(target, backup);
            try {
                File.Copy(self, target, true);
                using (var sha = SHA256.Create())
                using (var stream = File.OpenRead(target)) {
                    string hash = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
                    if (hash != PayloadHash) throw new InvalidDataException("替换后的便携版校验失败。");
                }
            } catch {
                try { if (File.Exists(target)) File.Delete(target); } catch { }
                File.Move(backup, target); // 回滚
                throw;
            }
            try { File.Delete(backup); } catch { }
            Process.Start(new ProcessStartInfo(target) { WorkingDirectory = Path.GetDirectoryName(target), UseShellExecute = false });
            return 0;
        } catch (Exception ex) {
            try {
                Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(target)));
                File.WriteAllText(target + ".update-error.txt",
                    DateTime.Now.ToString("s") + Environment.NewLine + ex);
            } catch { }
            MessageBox.Show("便携版更新失败，已保留原文件。\r\n\r\n" + ex.Message,
                "CapsWriter 便携版更新", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
    }

    [STAThread]
    static int Main(string[] args) {
        if (args.Length >= 2 && args[0] == "--replace-self") {
            int waitPid = 0;
            if (args.Length >= 4 && args[2] == "--wait-pid") int.TryParse(args[3], out waitPid);
            return ReplaceSelf(args[1], waitPid);
        }
        bool verify = args.Length == 2 && args[0] == "--verify-package";
        try {
            string root = verify ? args[1] : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CapsWriterOffline", "Portable");
            string cache = Extract(root);
            CleanupUpdateArtifacts();
            if (verify) { File.WriteAllText(Path.Combine(Path.GetFullPath(root), "verified-path.txt"), cache); return 0; }
            var psi = new ProcessStartInfo(Path.Combine(cache, "CapsWriterDesktop.exe")) {
                WorkingDirectory = cache, UseShellExecute = false
            };
            // 告诉 GUI 外层单文件 EXE 的位置与形态，程序内热更新据此替换正确的入口。
            string self = SelfExe;
            if (!string.IsNullOrEmpty(self)) {
                psi.EnvironmentVariables["CAPSWRITER_PORTABLE_EXE"] = Path.GetFullPath(self);
                psi.EnvironmentVariables["CAPSWRITER_PORTABLE_VERSION"] = Version;
            }
            var process = Process.Start(psi);
            if (process == null) throw new IOException("无法启动 CapsWriter。");
            return 0;
        } catch (Exception ex) {
            if (verify) {
                try { Directory.CreateDirectory(args[1]); File.WriteAllText(Path.Combine(args[1], "verify-error.txt"), ex.ToString()); } catch { }
            } else MessageBox.Show(ex.Message, "CapsWriter 便携版启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
