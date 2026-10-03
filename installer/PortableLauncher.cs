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

    // 清理本程序自身的更新残留。只按"自身完整外层文件名"精确识别：
    //   <自身文件名>.update-bak / .update-partial / .update-error.txt
    //   <自身文件名>.update-staging\（下载暂存目录，Update.cs 同名创建）
    // 禁止任何跨程序通配（*.update-* 曾误删其他程序的哨兵文件，
    // work/release-root-portable-cleanup.json），也不删无归属的
    // .update-staging 整目录。更新事务进行中（helper 还持有备份/留痕）
    // 或 --verify-package 诊断模式下一律跳过。
    static void CleanupUpdateArtifacts() {
        try {
            if (Environment.GetEnvironmentVariable("CAPSWRITER_UPDATE_IN_PROGRESS") == "1") return;
            string self = SelfExe;
            if (string.IsNullOrEmpty(self)) return;
            string dir = Path.GetDirectoryName(Path.GetFullPath(self));
            if (dir == null || !Directory.Exists(dir)) return;
            string ownPrefix = Path.GetFileName(self) + ".";
            string[] ownArtifacts = {
                ownPrefix + "update-bak", ownPrefix + "update-partial", ownPrefix + "update-error.txt"
            };
            foreach (string file in ownArtifacts) {
                string path = Path.Combine(dir, file);
                if (File.Exists(path)) { try { File.Delete(path); } catch { } }
            }
            string staging = Path.Combine(dir, ownPrefix + "update-staging");
            if (Directory.Exists(staging)) { try { Directory.Delete(staging, true); } catch { } }
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

    static string Sha256Hex(string path) {
        using (var sha = SHA256.Create())
        using (var stream = File.OpenRead(path)) {
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }
    }

    // 诊断开关，仅供更新事务小样测试使用（生产环境永不设置；环境变量随
    // 进程继承，测试用它控制"被启动的新版本"行为，全程不开 GUI/不录音）：
    //   1          = Extract 成功后直接 exit 0（解压本身即启动成功证据）
    //   fail-start = Extract 成功后 exit 3，模拟应用启动失败
    //   fail-partial = 暂存写入并校验后、原子替换前 exit 4（证明部分写入不碰旧 EXE）
    static string DiagMode {
        get { return Environment.GetEnvironmentVariable("CAPSWRITER_UPDATE_DIAGNOSTIC"); }
    }

    // --replace-self <旧EXE路径> [--wait-pid <pid>] 更新事务核心（生产路径）：
    // 1. 等待旧 GUI 退出（pid 已不存在 = 已退出，继续）；
    // 2. 新版本先完整写入同目录 .update-partial 并逐字节校验——此阶段旧 EXE
    //    未被触碰，崩溃/部分写入/校验失败都绝不影响当前可用程序；
    // 3. File.Replace 一步原子替换：新版本生效，旧版本完整落入 .update-bak；
    // 4. 启动新版本并观察退出码（解压/启动失败 60s 内以非零退出）；
    // 5. 任一步失败都从备份恢复旧版并重新启动旧版，成功才删除备份。
    // 嵌入 ZIP 的 PayloadHash 只在 Extract 内校验；此处比较的是
    // "写入文件 vs 已验证下载 EXE"同一对象。
    static int ReplaceSelf(string target, int waitPid) {
        string backup = null;
        string partial = null;
        try {
            target = Path.GetFullPath(target);
            if (!File.Exists(target)) throw new FileNotFoundException("找不到要替换的便携版 EXE。", target);
            if (waitPid > 0 && WaitForExit(waitPid, 120) != 0)
                throw new IOException("等待旧界面退出超时，已取消更新。");
            string self = Path.GetFullPath(SelfExe);
            string selfHash = Sha256Hex(self);
            partial = target + ".update-partial";
            try { if (File.Exists(partial)) File.Delete(partial); } catch { }
            File.Copy(self, partial, true);
            if (Sha256Hex(partial) != selfHash)
                throw new InvalidDataException("新版本暂存写入校验失败，旧 EXE 未被改动。");
            if (DiagMode == "fail-partial") return 4;
            backup = target + ".update-bak";
            try { if (File.Exists(backup)) File.Delete(backup); } catch { }
            File.Replace(partial, target, backup);
            partial = null;
            try {
                var psi = new ProcessStartInfo(target) {
                    WorkingDirectory = Path.GetDirectoryName(target), UseShellExecute = false
                };
                // 标记"更新事务进行中"：被启动的启动器据此跳过更新残留清理，
                // 否则它会把本 helper 回滚所需的 .update-bak 提前删掉
                psi.EnvironmentVariables["CAPSWRITER_UPDATE_IN_PROGRESS"] = "1";
                var started = Process.Start(psi);
                if (started == null) throw new IOException("新便携版未能启动。");
                bool failed = started.WaitForExit(60000) && started.ExitCode != 0;
                if (failed)
                    throw new IOException("新便携版启动后自行退出（ExitCode " + started.ExitCode + "），已回滚。");
            } catch {
                try { File.Replace(backup, target, null); }
                catch {
                    try { if (File.Exists(target)) File.Delete(target); } catch { }
                    File.Move(backup, target);
                }
                backup = null;
                var psi2 = new ProcessStartInfo(target) {
                    WorkingDirectory = Path.GetDirectoryName(target), UseShellExecute = false
                };
                // 回滚重启同样处于更新事务收尾阶段：跳过残留清理，避免吃掉
                // 错误留痕与暂存文件；下次正常启动再清
                psi2.EnvironmentVariables["CAPSWRITER_UPDATE_IN_PROGRESS"] = "1";
                try {
                    Process.Start(psi2);
                } catch { }
                throw;
            }
            try { if (File.Exists(backup)) File.Delete(backup); } catch { }
            backup = null;
            return 0;
        } catch (Exception ex) {
            try { if (partial != null && File.Exists(partial)) File.Delete(partial); } catch { }
            try {
                Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(target)));
                File.WriteAllText(target + ".update-error.txt",
                    DateTime.Now.ToString("s") + Environment.NewLine + ex);
            } catch { }
            if (DiagMode == null) {
                MessageBox.Show("便携版更新失败，已恢复原文件。\r\n\r\n" + ex.Message,
                    "CapsWriter 便携版更新", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
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
            if (verify) { File.WriteAllText(Path.Combine(Path.GetFullPath(root), "verified-path.txt"), cache); return 0; }
            CleanupUpdateArtifacts();
            string diag = DiagMode;
            if (diag == "1") return 0;          // 更新事务小样：解压成功即启动成功
            if (diag == "fail-start") return 3; // 更新事务小样：可控启动失败
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
