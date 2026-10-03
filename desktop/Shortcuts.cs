using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;

internal static partial class Desktop {
    static CheckBox holdHotkey, toggleHotkey;
    static Button holdKeyButton, toggleKeyButton, captureTarget;
    static TextBlock captureHint;
    static string holdKeyName="caps_lock", toggleKeyName="ctrl+alt+space";
    static readonly HashSet<string> captureModifiers=new HashSet<string>();
    static DispatcherTimer captureTimer;
    static DispatcherTimer shortcutApplyTimer;   // 快捷键变更防抖：录入/开关后统一走一次保存+应用
    static bool capturePaused;

    static UIElement ShortcutSettingsPanel() {
        var panel=new StackPanel { Margin=new Thickness(0,13,0,0) };
        panel.Children.Add(Text("录音快捷键",11,"#747679","#A7A9AB"));
        holdHotkey=Switch("启用");
        toggleHotkey=Switch("启用");
        holdHotkey.IsChecked=true;
        toggleHotkey.IsChecked=true;
        holdKeyButton=ShortcutCaptureButton();
        toggleKeyButton=ShortcutCaptureButton();
        holdKeyButton.ToolTip="按住说话，松开后结束录音并识别。";
        toggleKeyButton.ToolTip="按一次开始，再按一次结束并识别。";
        holdKeyButton.Click+=(s,e)=>BeginShortcutCapture(holdKeyButton);
        toggleKeyButton.Click+=(s,e)=>BeginShortcutCapture(toggleKeyButton);
        // 启用开关立即持久化并应用（防抖合并连点）；启动装配期间由 mainLoaded 拦下
        holdHotkey.Checked+=(s,e)=>ScheduleShortcutApply();
        holdHotkey.Unchecked+=(s,e)=>ScheduleShortcutApply();
        toggleHotkey.Checked+=(s,e)=>ScheduleShortcutApply();
        toggleHotkey.Unchecked+=(s,e)=>ScheduleShortcutApply();
        panel.Children.Add(ShortcutRow("长按说话",holdKeyButton,holdHotkey));
        panel.Children.Add(ShortcutRow("按键开关",toggleKeyButton,toggleHotkey));
        captureHint=Text("点击按键框后按下键或组合键；Esc 取消。",11,"#77797C","#A7A9AB");
        captureHint.Margin=new Thickness(0,8,0,0);
        panel.Children.Add(captureHint);
        UpdateShortcutButtons();
        main.PreviewKeyDown+=CapturePreviewKeyDown;
        main.PreviewKeyUp+=CapturePreviewKeyUp;
        main.Deactivated+=(s,e)=>EndShortcutCapture();
        main.PreviewMouseDown+=(s,e)=>{
            if(captureTarget!=null && !captureTarget.IsMouseOver)EndShortcutCapture();
        };
        captureTimer=new DispatcherTimer { Interval=TimeSpan.FromSeconds(25) };
        captureTimer.Tick+=(s,e)=>EndShortcutCapture();
        shortcutApplyTimer=new DispatcherTimer { Interval=TimeSpan.FromMilliseconds(500) };
        shortcutApplyTimer.Tick+=(s,e)=>{
            shortcutApplyTimer.Stop();
            ApplyShortcutChanges();
        };
        return panel;
    }

    static Grid ShortcutRow(string label,Button keyButton,CheckBox enabled) {
        var row=new Grid { Margin=new Thickness(0,8,0,0) };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(96) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(80) });
        row.Children.Add(Text(label,12,"#303235","#E6E7E6",true));
        keyButton.HorizontalAlignment=HorizontalAlignment.Stretch;
        Grid.SetColumn(keyButton,1);row.Children.Add(keyButton);
        enabled.Margin=new Thickness(12,0,0,0);
        Grid.SetColumn(enabled,2);row.Children.Add(enabled);
        return row;
    }

    static Button ShortcutCaptureButton() {
        var button=ThemeButton("","#F6F6F4","#292A2D","#252628","#EEEEEC",8);
        button.Height=36;
        button.HorizontalContentAlignment=HorizontalAlignment.Left;
        button.Padding=new Thickness(12,0,10,0);
        return button;
    }

    static void UpdateShortcutButtons() {
        if(holdKeyButton!=null)holdKeyButton.Content=FormatShortcut(holdKeyName);
        if(toggleKeyButton!=null)toggleKeyButton.Content=FormatShortcut(toggleKeyName);
    }

    static string FormatShortcut(string key) {
        var parts=key.Split('+');
        for(int i=0;i<parts.Length;i++) {
            switch(parts[i]) {
                case "ctrl": parts[i]="Ctrl"; break;
                case "alt": parts[i]="Alt"; break;
                case "shift": parts[i]="Shift"; break;
                case "caps_lock": parts[i]="Caps Lock"; break;
                case "space": parts[i]="Space"; break;
                default: parts[i]=parts[i].ToUpperInvariant(); break;
            }
        }
        return String.Join(" + ",parts);
    }

    static string ShortcutHint() {
        var parts=new List<string>();
        if(holdHotkey!=null && holdHotkey.IsChecked==true)
            parts.Add("长按 "+FormatShortcut(holdKeyName));
        if(toggleHotkey!=null && toggleHotkey.IsChecked==true)
            parts.Add("按 "+FormatShortcut(toggleKeyName)+" 开关录音");
        return parts.Count==0?"点击浮窗录音":String.Join("，",parts.ToArray());
    }

    // 字典项取值：引号值按 Python 字面量解析（保留转义），裸 token 在
    // 行尾注释(#)、逗号、右花括号处截断——默认配置里 'enabled': True 后
    // 面带行内注释，旧正则会把注释一起吃进导致 True 被误判为 False。
    static string EntryValue(string entry,string name,string fallback) {
        var match=Regex.Match(entry,@"'"+Regex.Escape(name)+@"'\s*:\s*");
        if(!match.Success)return fallback;
        int i=match.Index+match.Length, n=entry.Length;
        while(i<n && char.IsWhiteSpace(entry[i]))i++;
        if(i>=n)return fallback;
        var sb=new System.Text.StringBuilder();
        if(entry[i]=='\''||entry[i]=='"') {
            char quote=entry[i++];
            while(i<n && entry[i]!=quote) {
                if(entry[i]=='\\' && i+1<n) { sb.Append(entry[i]).Append(entry[i+1]); i+=2; }
                else sb.Append(entry[i++]);
            }
            if(i>=n)return fallback;      // 引号未闭合
            i++;
        } else {
            while(i<n) {
                char c=entry[i];
                if(c=='\r'||c=='\n'||c==','||c=='}'||c=='#')break;
                sb.Append(c); i++;
            }
        }
        string token=sb.ToString().Trim();
        return token.Length>0?token:fallback;
    }

    static string ShortcutBlock(string content) {
        var match=Regex.Match(content,@"(?ms)^[ ]{4}shortcuts\s*=\s*\[(.*?)^[ ]{4}\]");
        return match.Success?match.Groups[1].Value:"";
    }

    static void LoadShortcutSettings(string content) {
        holdKeyName="caps_lock";toggleKeyName="ctrl+alt+space";
        bool holdEnabled=true,toggleEnabled=true;
        bool foundHold=false,foundToggle=false;
        foreach(Match match in Regex.Matches(ShortcutBlock(content),@"\{[^{}]*\}")) {
            string entry=match.Value;
            if(EntryValue(entry,"type","keyboard")!="keyboard")continue;
            bool isHold=EntryValue(entry,"hold_mode","True")=="True";
            string key=EntryValue(entry,"key",isHold?"caps_lock":"ctrl+alt+space");
            bool enabled=EntryValue(entry,"enabled","True")=="True";
            if(isHold && !foundHold) {
                holdKeyName=key;holdEnabled=enabled;foundHold=true;
            } else if(!isHold && !foundToggle) {
                toggleKeyName=key;toggleEnabled=enabled;foundToggle=true;
            }
        }
        holdHotkey.IsChecked=holdEnabled;
        toggleHotkey.IsChecked=toggleEnabled;
        UpdateShortcutButtons();
    }

    static bool ValidateShortcutSettings() {
        if(!ValidShortcut(holdKeyName) || !ValidShortcut(toggleKeyName)) {
            MessageBox.Show("快捷键配置包含不支持的按键，请重新录入。");
            return false;
        }
        if(holdHotkey.IsChecked==true && toggleHotkey.IsChecked==true &&
           String.Equals(holdKeyName,toggleKeyName,StringComparison.OrdinalIgnoreCase)) {
            MessageBox.Show("长按和按键开关不能使用同一个快捷键。请修改其中一项，或关闭其中一项。");
            return false;
        }
        return true;
    }

    static bool ValidShortcut(string key) {
        var parts=key.Split('+');
        var seen=new HashSet<string>();
        for(int i=0;i<parts.Length-1;i++) {
            if((parts[i]!="ctrl" && parts[i]!="alt" && parts[i]!="shift") ||
               !seen.Add(parts[i]))return false;
        }
        string trigger=parts[parts.Length-1];
        return Regex.IsMatch(trigger,@"^(?:caps_lock|ctrl|alt|shift|space|tab|enter|delete|f(?:[1-9]|1[0-9]|2[0-4])|[a-z0-9])$") &&
            !seen.Contains(trigger) &&
            (parts.Length>1 || !Regex.IsMatch(trigger,@"^[a-z0-9]$"));
    }

    static bool SuppressShortcut(string key) {
        return key!="ctrl" && key!="alt" && key!="shift";
    }

    static string ReplaceShortcutBlock(string content) {
        var match=Regex.Match(content,@"(?ms)^[ ]{4}shortcuts\s*=\s*\[(.*?)^[ ]{4}\]");
        if(!match.Success)throw new InvalidOperationException("config_client.py 中找不到 shortcuts 配置。");
        string nl=content.Contains("\r\n")?"\r\n":"\n";
        var block=new StringBuilder();
        block.Append("    shortcuts = [").Append(nl);
        AppendShortcut(block,nl,holdKeyName,true,holdHotkey.IsChecked==true);
        AppendShortcut(block,nl,toggleKeyName,false,toggleHotkey.IsChecked==true);
        foreach(Match entry in Regex.Matches(match.Groups[1].Value,@"\{[^{}]*\}")) {
            if(EntryValue(entry.Value,"type","keyboard")=="mouse")
                block.Append("        ").Append(entry.Value.Trim()).Append(",").Append(nl);
        }
        block.Append("    ]");
        return content.Substring(0,match.Index)+block.ToString()+content.Substring(match.Index+match.Length);
    }

    static void AppendShortcut(StringBuilder block,string nl,string key,bool hold,bool enabled) {
        block.Append("        {'key': '").Append(key)
            .Append("', 'type': 'keyboard', 'suppress': ")
            .Append(SuppressShortcut(key)?"True":"False")
            .Append(", 'hold_mode': ").Append(hold?"True":"False")
            .Append(", 'enabled': ").Append(enabled?"True":"False")
            .Append("},").Append(nl);
    }

    static bool PauseBackendShortcuts() {
        if(backend==null || backend.HasExited)return true;
        try {
            using(var udp=new UdpClient()) {
                udp.Client.ReceiveTimeout=900;
                var bytes=Encoding.ASCII.GetBytes("PAUSE_HOTKEYS");
                udp.Send(bytes,bytes.Length,new IPEndPoint(IPAddress.Loopback,6018));
                IPEndPoint remote=new IPEndPoint(IPAddress.Any,0);
                return Encoding.ASCII.GetString(udp.Receive(ref remote))=="PAUSED";
            }
        } catch(SocketException) { return false; }
    }

    static void EndShortcutCapture() {
        if(captureTarget==null)return;
        captureTarget=null;
        captureModifiers.Clear();
        if(captureTimer!=null)captureTimer.Stop();
        if(capturePaused) {
            capturePaused=false;
            try { SendControl("RESUME_HOTKEYS"); } catch(SocketException) {}
        }
        captureHint.Text="点击按键框后按下键或组合键；Esc 取消。";
        UpdateShortcutButtons();
    }

    static void BeginShortcutCapture(Button button) {
        if(recording || processing) {
            MessageBox.Show("请先结束录音和识别，再设置快捷键。");
            return;
        }
        EndShortcutCapture();
        if(!PauseBackendShortcuts()) {
            MessageBox.Show("客户端未确认暂停热键，请稍后重试快捷键录入。");
            return;
        }
        capturePaused=backend!=null && !backend.HasExited;
        captureTarget=button;
        captureModifiers.Clear();
        button.Content="请按快捷键…";
        captureHint.Text="可按 Ctrl、Alt、Shift 单键或组合键；Esc 取消。";
        captureTimer.Start();
        button.Focus();
    }

    static string KeyName(Key key) {
        if(key==Key.LeftCtrl || key==Key.RightCtrl)return "ctrl";
        if(key==Key.LeftAlt || key==Key.RightAlt)return "alt";
        if(key==Key.LeftShift || key==Key.RightShift)return "shift";
        if(key==Key.Capital)return "caps_lock";
        if(key==Key.Space)return "space";
        if(key==Key.Tab)return "tab";
        if(key==Key.Enter || key==Key.Return)return "enter";
        if(key==Key.Delete)return "delete";
        if(key>=Key.F1 && key<=Key.F24)return "f"+((int)key-(int)Key.F1+1).ToString();
        if(key>=Key.A && key<=Key.Z)return ((char)('a'+(int)key-(int)Key.A)).ToString();
        if(key>=Key.D0 && key<=Key.D9)return ((int)key-(int)Key.D0).ToString();
        return null;
    }

    static string EventKey(KeyEventArgs e) {
        return KeyName(e.Key==Key.System?e.SystemKey:e.Key);
    }

    static void CapturePreviewKeyDown(object sender,KeyEventArgs e) {
        if(captureTarget==null)return;
        e.Handled=true;
        if(e.Key==Key.Escape) { EndShortcutCapture();return; }
        string key=EventKey(e);
        if(key==null) {
            captureHint.Text="此键暂不支持，请按其他键；Esc 取消。";
            return;
        }
        if(key=="ctrl" || key=="alt" || key=="shift") {
            captureModifiers.Add(key);
            return;
        }
        if(key.Length==1 && captureModifiers.Count==0) {
            captureHint.Text="字母和数字需与 Ctrl、Alt 或 Shift 组合。";
            return;
        }
        CompleteShortcutCapture(CombineShortcut(key));
    }

    static void CapturePreviewKeyUp(object sender,KeyEventArgs e) {
        if(captureTarget==null)return;
        e.Handled=true;
        string key=EventKey(e);
        if(key==null || !captureModifiers.Contains(key))return;
        if(captureModifiers.Count==1)
            CompleteShortcutCapture(key);
        else
            captureModifiers.Remove(key);
    }

    static string CombineShortcut(string key) {
        var parts=new List<string>();
        if(captureModifiers.Contains("ctrl"))parts.Add("ctrl");
        if(captureModifiers.Contains("alt"))parts.Add("alt");
        if(captureModifiers.Contains("shift"))parts.Add("shift");
        parts.Add(key);
        return String.Join("+",parts.ToArray());
    }

    static void CompleteShortcutCapture(string key) {
        bool hold=captureTarget==holdKeyButton;
        string other=hold?toggleKeyName:holdKeyName;
        bool otherEnabled=hold?toggleHotkey.IsChecked==true:holdHotkey.IsChecked==true;
        bool thisEnabled=hold?holdHotkey.IsChecked==true:toggleHotkey.IsChecked==true;
        if(thisEnabled && otherEnabled && key==other) {
            captureHint.Text="两种模式不能使用同一个快捷键，请换一个键。";
            return;
        }
        if(hold)holdKeyName=key;
        else toggleKeyName=key;
        EndShortcutCapture();
        ScheduleShortcutApply();
    }

    // ===== 快捷键变更的立即保存与应用 =====

    static void ScheduleShortcutApply() {
        // 启动装配（LoadSettings 重建开关状态）期间触发的事件不是用户变更
        if(!mainLoaded)return;
        if(shortcutApplyTimer==null)return;
        shortcutApplyTimer.Stop();
        shortcutApplyTimer.Start();
    }

    // 快捷键设置立即生效：写盘（含 .bak 链路与回读校验）→ 通知运行中的
    // 客户端热重载（RELOAD_HOTKEYS，客户端先按静音生命周期结束录音）；
    // 热重载未确认且客户端归本程序管理时重启客户端；失败都给出明确提示。
    static void ApplyShortcutChanges() {
        if(!mainLoaded)return;
        if(!ValidateShortcutSettings()) { ReloadShortcutSettingsFromDisk(); return; }
        string content=ReadConfig();
        string updated;
        try { updated=ReplaceShortcutBlock(content); }
        catch(InvalidOperationException ex) {
            MessageBox.Show("快捷键保存失败："+ex.Message);
            return;
        }
        string invalid=ValidateShortcutBlock(updated);
        if(invalid!=null) {
            MessageBox.Show("快捷键回读校验失败（"+invalid+"），已放弃写入；原配置未改动。");
            ReloadShortcutSettingsFromDisk();
            return;
        }
        if(updated!=content) {
            try {
                File.Copy(Config,Config+".bak",true);
                File.WriteAllText(Config,updated,new UTF8Encoding(false));
            } catch(Exception ex) {
                MessageBox.Show("快捷键配置写入失败："+ex.Message);
                return;
            }
        }
        ApplyShortcutToBackend();
    }

    // 写盘后的回读校验：shortcuts 块里 keyboard 项的 key/hold_mode/enabled
    // 必须与界面一致；mouse 项数量与写盘前一致（ReplaceShortcutBlock 原样
    // 保留其内容）。返回 null 表示通过。
    static string ValidateShortcutBlock(string updated) {
        string block=ShortcutBlock(updated);
        if(block.Length==0)return "shortcuts 配置块缺失";
        bool foundHold=false,foundToggle=false; int mouse=0;
        foreach(Match entry in Regex.Matches(block,@"\{[^{}]*\}")) {
            string e=entry.Value;
            if(EntryValue(e,"type","keyboard")=="mouse") { mouse++; continue; }
            bool isHold=EntryValue(e,"hold_mode","True")=="True";
            string key=EntryValue(e,"key","");
            if(key.Length==0)return "key 字段缺失";
            bool enabled=EntryValue(e,"enabled","")=="True";
            if(isHold && !foundHold) {
                foundHold=true;
                if(key!=holdKeyName || enabled!=(holdHotkey.IsChecked==true))return "长按项回读不一致";
            } else if(!isHold && !foundToggle) {
                foundToggle=true;
                if(key!=toggleKeyName || enabled!=(toggleHotkey.IsChecked==true))return "开关项回读不一致";
            }
        }
        if(!foundHold || !foundToggle)return "keyboard 配置项缺失";
        int mouseBefore=0;
        string current=ReadConfig();
        var prevMatch=Regex.Match(current,@"(?ms)^[ ]{4}shortcuts\s*=\s*\[(.*?)^[ ]{4}\]");
        if(prevMatch.Success)
            foreach(Match entry in Regex.Matches(prevMatch.Groups[1].Value,@"\{[^{}]*\}"))
                if(EntryValue(entry.Value,"type","keyboard")=="mouse")mouseBefore++;
        if(mouse!=mouseBefore)return "mouse 配置项丢失";
        return null;
    }

    static void ReloadShortcutSettingsFromDisk() {
        try { LoadShortcutSettings(ReadConfig()); } catch(System.IO.IOException) {}
    }

    static void ApplyShortcutToBackend() {
        if(backend==null || backend.HasExited) {
            status.Text="快捷键已保存（客户端未运行，下次启动生效）";
            return;
        }
        if(recording) { try { SendControl("STOP"); } catch(SocketException) {} }
        try {
            using(var udp=new UdpClient()) {
                udp.Client.ReceiveTimeout=2000;
                var bytes=Encoding.ASCII.GetBytes("RELOAD_HOTKEYS");
                udp.Send(bytes,bytes.Length,new IPEndPoint(IPAddress.Loopback,6018));
                var deadline=Environment.TickCount+4000;
                while(Environment.TickCount<deadline) {
                    var remote=new IPEndPoint(IPAddress.Loopback,0);
                    byte[] data;
                    try { data=udp.Receive(ref remote); }
                    catch(SocketException) { break; }
                    var text=Encoding.ASCII.GetString(data);
                    if(text=="RELOADED") {
                        status.Text="快捷键已保存并生效";
                        return;
                    }
                    if(text.StartsWith("RELOAD_FAILED"))break;
                }
            }
        } catch(SocketException) {}
        // 热重载未确认：客户端归本程序管理时重启使其生效（RestartBackend
        // 内部含静音恢复握手，未确认会中止并提示）。进程重启本身不算生效
        // 凭证——必须在新客户端日志中确认启用的快捷键完成登记才算成功。
        if(!ownsBackend) {
            status.Text="快捷键已保存，但客户端热重载未确认，请重试或重启客户端";
            return;
        }
        long logBefore=LogLength();
        if(!RestartBackend()) {
            status.Text="快捷键已保存，但客户端重启未确认，请检查客户端";
            return;
        }
        status.Text=ConfirmShortcutLogRegistration(logBefore)
            ?"快捷键已保存，客户端已重启生效"
            :"快捷键已保存，客户端已重启，但未确认新快捷键登记，请检查";
    }

    static long LogLength() {
        try {
            using(var f=new FileStream(Log,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
                return f.Length;
        } catch(IOException) { return 0; } catch(UnauthorizedAccessException) { return 0; }
    }

    static string ReadLogRange(long from) {
        try {
            using(var f=new FileStream(Log,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete)) {
                if(f.Length<=from)return "";
                f.Seek(from,SeekOrigin.Begin);
                using(var r=new StreamReader(f,Encoding.UTF8,true,4096,true)) return r.ReadToEnd();
            }
        } catch(IOException) { return ""; } catch(UnauthorizedAccessException) { return ""; }
    }

    // 等待新客户端日志出现启用快捷键的登记行（"  [key] 模式…"，仅登记
    // 已启用的绑定）；超时返回 false——重启后不得宣称快捷键已生效。
    static bool ConfirmShortcutLogRegistration(long fromLength) {
        var keys=new List<string>();
        if(holdHotkey.IsChecked==true)keys.Add(holdKeyName);
        if(toggleHotkey.IsChecked==true)keys.Add(toggleKeyName);
        if(keys.Count==0)return true;
        var deadline=Environment.TickCount+15000;
        while(Environment.TickCount<deadline) {
            string text=ReadLogRange(fromLength);
            bool all=true;
            foreach(var k in keys)
                if(!Regex.IsMatch(text,Regex.Escape("["+k)+"]"+@"(?!\w)")) { all=false; break; }
            if(all)return true;
            System.Threading.Thread.Sleep(300);
        }
        return false;
    }
}
