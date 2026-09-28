using System;
using System.Diagnostics;

using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Effects;
using System.Windows.Shapes;
using System.Windows.Threading;
using Forms = System.Windows.Forms;
using Path = System.IO.Path;

internal static class Desktop {
    [System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint="GetWindowLongW")]
    static extern int GetWindowLong(IntPtr h, int index);
    [System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint="SetWindowLongW")]
    static extern int SetWindowLong(IntPtr h, int index, int value);

    static readonly string Dir = AppDomain.CurrentDomain.BaseDirectory;
    static readonly string Exe = Path.Combine(Dir, "start_client.exe");
    static readonly string Config = Path.Combine(Dir, "config_client.py");
    static readonly string Log = Path.Combine(Dir, "logs", "client_latest.log");
    static Mutex single;
    static Window main, floatWindow;
    static Forms.NotifyIcon tray;
    static Process backend;
    static DispatcherTimer tick;
    static TextBox host, port, seconds, logBox;
    static TextBlock status, transcript, floatStatus, floatDetail;
    static Button mainRecord, floatRecord;
    static CheckBox showFloat, capsHotkey;
    static bool connected, recording, processing, exiting, ownsBackend;
    static string lastText = "";
    static DateTime started;
    static long logPosition;
    static string partialLine = "";

    [STAThread]
    static void Main() {
        bool first;
        single = new Mutex(true, @"Local\CapsWriterUnifiedDesktop", out first);
        if (!first) { MessageBox.Show("CapsWriter 已在运行。请从托盘打开主界面。"); return; }
        try {
            if (!File.Exists(Exe) || !File.Exists(Config))
                throw new FileNotFoundException("请把 CapsWriterDesktop.exe 放在 start_client.exe 和 config_client.py 旁边。");
            var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            BuildMain();
            BuildFloat();
            BuildTray();
            LoadSettings();
            SaveSettings(false);
            StartBackend();
            main.Show();
            if (showFloat.IsChecked == true) floatWindow.Show();
            tick = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
            tick.Tick += (s,e) => { ReadLog(); UpdateDisplay(); };
            tick.Start();
            app.Run();
        } catch (Exception ex) {
            MessageBox.Show(ex.ToString(), "CapsWriter 启动失败");
        } finally {
            Exit();
            single.ReleaseMutex();
            single.Dispose();
        }
    }

    static Brush B(string hex) { return (Brush)new BrushConverter().ConvertFromString(hex); }
    static TextBlock Label(string value, int size=13, string color="#C5D1DF") {
        return new TextBlock { Text=value, FontSize=size, Foreground=B(color), VerticalAlignment=VerticalAlignment.Center };
    }
    static Button Btn(string value, string bg="#3B70E6") {
        var button = new Button { Content=value, Height=38, Padding=new Thickness(15,0,15,0),
            Background=B(bg), Foreground=Brushes.White, BorderThickness=new Thickness(0),
            FontWeight=FontWeights.SemiBold, Cursor=Cursors.Hand, Margin=new Thickness(0,0,8,0) };
        var border = new FrameworkElementFactory(typeof(Border));
        border.SetValue(Border.BackgroundProperty, new System.Windows.Data.Binding("Background") {
            RelativeSource=new System.Windows.Data.RelativeSource(System.Windows.Data.RelativeSourceMode.TemplatedParent) });
        border.SetValue(Border.CornerRadiusProperty, new CornerRadius(9));
        var content = new FrameworkElementFactory(typeof(ContentPresenter));
        content.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Center);
        content.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
        border.AppendChild(content);
        button.Template=new ControlTemplate(typeof(Button)) { VisualTree=border };
        return button;
    }
    static TextBox Field(int width) {
        return new TextBox { Width=width, Height=36, FontSize=14, Foreground=Brushes.White,
            Background=B("#263449"), BorderBrush=B("#41536A"), BorderThickness=new Thickness(1),
            Padding=new Thickness(9,6,9,4), Margin=new Thickness(0,5,12,0) };
    }
    static Border Card(UIElement child) {
        return new Border { Background=B("#1D2A3B"), CornerRadius=new CornerRadius(14),
            Padding=new Thickness(18), Margin=new Thickness(0,0,0,14), Child=child };
    }
    static void BuildMain() {
        main=new Window { Title="CapsWriter · 语音输入", Width=760, Height=650, MinWidth=610, MinHeight=530,
            WindowStartupLocation=WindowStartupLocation.CenterScreen, Background=B("#111B2A"),
            FontFamily=new FontFamily("Microsoft YaHei UI") };
        main.Closed += (s,e) => { if (!exiting) main.Hide(); };
        main.Closing += (s,e) => { if (!exiting) { e.Cancel=true; main.Hide(); } };
        var root=new Grid { Margin=new Thickness(24) };
        root.RowDefinitions.Add(new RowDefinition { Height=GridLength.Auto });
        root.RowDefinitions.Add(new RowDefinition { Height=new GridLength(1,GridUnitType.Star) });
        var heading=new StackPanel { Margin=new Thickness(0,0,0,20) };
        heading.Children.Add(Label("CapsWriter",27,"#F4F8FE"));
        status=Label("正在启动客户端…",13,"#93A7C2");
        status.Margin=new Thickness(0,6,0,0);
        heading.Children.Add(status);
        Grid.SetRow(heading,0); root.Children.Add(heading);
        var scroll=new ScrollViewer { VerticalScrollBarVisibility=ScrollBarVisibility.Auto };
        var body=new StackPanel();
        var network=new StackPanel();
        network.Children.Add(Label("服务器连接",17,"#F4F8FE"));
        var row=new StackPanel { Orientation=Orientation.Horizontal, Margin=new Thickness(0,10,0,8) };
        var a=new StackPanel(); a.Children.Add(Label("地址")); host=Field(285); a.Children.Add(host); row.Children.Add(a);
        var p=new StackPanel(); p.Children.Add(Label("端口")); port=Field(105); p.Children.Add(port); row.Children.Add(p);
        var d=new StackPanel(); d.Children.Add(Label("分段秒数")); seconds=Field(105); d.Children.Add(seconds); row.Children.Add(d);
        network.Children.Add(row);
        network.Children.Add(Label("每段识别后直接输入当前应用。分段越短，响应越快，但准确率可能下降。",12,"#92A7C1"));
        var controls=new StackPanel { Orientation=Orientation.Horizontal, Margin=new Thickness(0,15,0,0) };
        var save=Btn("保存并重连"); save.Click+=(s,e)=>SaveSettings(true); controls.Children.Add(save);
        var restart=Btn("重启客户端","#384B66"); restart.Click+=(s,e)=>RestartBackend(); controls.Children.Add(restart);
        network.Children.Add(controls);
        body.Children.Add(Card(network));

        var recordingCard=new StackPanel();
        recordingCard.Children.Add(Label("录音控制",17,"#F4F8FE"));
        var controlRow=new StackPanel { Orientation=Orientation.Horizontal, Margin=new Thickness(0,14,0,4) };
        mainRecord=Btn("开始录音"); mainRecord.Width=150; mainRecord.Height=46;
        mainRecord.Click+=(s,e)=>{ bool wasRecording=recording; ToggleRecording(); if(!wasRecording && recording==false) main.WindowState=WindowState.Minimized; };
        controlRow.Children.Add(mainRecord);
        showFloat=new CheckBox { Content="显示浮窗", Foreground=B("#DDE7F5"), VerticalAlignment=VerticalAlignment.Center,
            Margin=new Thickness(15,0,20,0) };
        showFloat.Checked+=(s,e)=>{ if (floatWindow!=null) floatWindow.Show(); };
        showFloat.Unchecked+=(s,e)=>{ if (floatWindow!=null) floatWindow.Hide(); };
        controlRow.Children.Add(showFloat);
        capsHotkey=new CheckBox { Content="CapsLock 长按", Foreground=B("#DDE7F5"), VerticalAlignment=VerticalAlignment.Center };
        controlRow.Children.Add(capsHotkey);
        recordingCard.Children.Add(controlRow);
        recordingCard.Children.Add(Label("点浮窗或主界面的按钮开始/停止，也可长按 CapsLock。",12,"#92A7C1"));
        body.Children.Add(Card(recordingCard));

        var resultCard=new StackPanel();
        resultCard.Children.Add(Label("当前识别",17,"#F4F8FE"));
        transcript=Label("等待录音",15,"#C5D1DF");
        transcript.TextWrapping=TextWrapping.Wrap;
        transcript.Margin=new Thickness(0,12,0,0);
        resultCard.Children.Add(transcript);
        body.Children.Add(Card(resultCard));

        var logCard=new StackPanel();
        logCard.Children.Add(Label("运行日志",17,"#F4F8FE"));
        logBox=new TextBox { Height=160, Margin=new Thickness(0,12,0,0), IsReadOnly=true,
            TextWrapping=TextWrapping.NoWrap, VerticalScrollBarVisibility=ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility=ScrollBarVisibility.Auto, Background=B("#142033"),
            Foreground=B("#AFC2DC"), BorderThickness=new Thickness(0), Padding=new Thickness(10),
            FontFamily=new FontFamily("Consolas"), FontSize=11 };
        logCard.Children.Add(logBox);
        body.Children.Add(Card(logCard));
        scroll.Content=body; Grid.SetRow(scroll,1); root.Children.Add(scroll);
        main.Content=root;
    }

    static void BuildFloat() {
        floatWindow=new Window { Width=350, Height=88, WindowStyle=WindowStyle.None,
            ResizeMode=ResizeMode.NoResize, AllowsTransparency=true, Background=Brushes.Transparent,
            Topmost=true, ShowInTaskbar=false, ShowActivated=false, FontFamily=new FontFamily("Microsoft YaHei UI") };
        var work=SystemParameters.WorkArea;
        floatWindow.Left=work.Right-floatWindow.Width-24;
        floatWindow.Top=work.Bottom-floatWindow.Height-24;
        floatWindow.SourceInitialized+=(s,e)=>{
            var h=new WindowInteropHelper(floatWindow).Handle;
            SetWindowLong(h,-20,GetWindowLong(h,-20)|0x08000000|0x80);
        };
        floatWindow.Closing+=(s,e)=>{ if(!exiting){e.Cancel=true;floatWindow.Hide();showFloat.IsChecked=false;} };
        var border=new Border { Background=B("#1B293D"), BorderBrush=B("#4A5D76"),
            BorderThickness=new Thickness(1), CornerRadius=new CornerRadius(17), Padding=new Thickness(13),
            Effect=new DropShadowEffect { Color=Colors.Black, Opacity=.4, BlurRadius=18, ShadowDepth=4 } };
        var grid=new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(96) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(21) });
        var labels=new StackPanel { VerticalAlignment=VerticalAlignment.Center };
        floatStatus=Label("连接中",15,"#F4F8FE");
        floatDetail=Label("",11,"#9CB0C8"); floatDetail.Margin=new Thickness(0,4,0,0);
        labels.Children.Add(floatStatus); labels.Children.Add(floatDetail);
        labels.MouseLeftButtonDown+=(s,e)=>{ try{floatWindow.DragMove();}catch{} };
        grid.Children.Add(labels);
        floatRecord=Btn("开始录音"); floatRecord.Margin=new Thickness(0); floatRecord.Click+=(s,e)=>ToggleRecording();
        Grid.SetColumn(floatRecord,1); grid.Children.Add(floatRecord);
        var close=Btn("×","#1B293D"); close.FontSize=18; close.Margin=new Thickness(2,-9,0,0);
        close.VerticalAlignment=VerticalAlignment.Top; close.Click+=(s,e)=>{floatWindow.Hide();showFloat.IsChecked=false;};
        Grid.SetColumn(close,2); grid.Children.Add(close);
        border.Child=grid; floatWindow.Content=border;
    }

    static void BuildTray() {
        var iconPath=Path.Combine(Dir,"assets","icon.ico");
        tray=new Forms.NotifyIcon { Visible=true, Text="CapsWriter 语音输入",
            Icon=File.Exists(iconPath)?new System.Drawing.Icon(iconPath):System.Drawing.SystemIcons.Information };
        var menu=new Forms.ContextMenuStrip();
        menu.Items.Add("打开主界面",null,(s,e)=>main.Dispatcher.Invoke(()=>{main.Show();main.Activate();}));
        menu.Items.Add("显示 / 隐藏浮窗",null,(s,e)=>main.Dispatcher.Invoke(()=>showFloat.IsChecked=showFloat.IsChecked!=true));
        menu.Items.Add("开始 / 停止录音",null,(s,e)=>main.Dispatcher.Invoke(()=>ToggleRecording()));
        menu.Items.Add("退出",null,(s,e)=>main.Dispatcher.Invoke(()=>{Exit();Application.Current.Shutdown();}));
        tray.ContextMenuStrip=menu;
        tray.DoubleClick+=(s,e)=>main.Dispatcher.Invoke(()=>{main.Show();main.Activate();});
    }

    static string ReadConfig() { return File.ReadAllText(Config,Encoding.UTF8); }
    static string Value(string source,string key,string fallback) {
        var m=Regex.Match(source,@"(?m)^\s*"+Regex.Escape(key)+@"\s*=\s*(.+?)(?:\s*#.*)?$");
        return m.Success ? m.Groups[1].Value.Trim().Trim('\'', '"') : fallback;
    }
    static void LoadSettings() {
        var content=ReadConfig();
        host.Text=Value(content,"addr","127.0.0.1");
        port.Text=Value(content,"port","6016");
        seconds.Text=Value(content,"mic_seg_duration","4");
        double previous;
        if (!Double.TryParse(seconds.Text, System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out previous) || previous>30) seconds.Text="4";
        showFloat.IsChecked=true;
        capsHotkey.IsChecked=!content.Contains("'key': 'caps_lock'") || !Regex.IsMatch(content,@"(?s)'key':\s*'caps_lock'.{0,180}?'enabled':\s*False");
    }
    static string Set(string source,string key,string value) {
        string pattern=@"(?m)^(\s*"+Regex.Escape(key)+@"\s*=\s*).*$";
        if(Regex.IsMatch(source,pattern)) return Regex.Replace(source,pattern,m=>m.Groups[1].Value+value,RegexOptions.Multiline);
        return source.Replace("class ClientConfig:", "class ClientConfig:\r\n    "+key+" = "+value);
    }
    static bool SaveSettings(bool restart) {
        string hostname=host.Text.Trim();
        ushort pn; double duration;
        if (hostname.Length==0 || hostname.Length>253 || !Regex.IsMatch(hostname,@"^[a-zA-Z0-9.:-]+$") ||
            !UInt16.TryParse(port.Text.Trim(),out pn) || pn==0 ||
            !Double.TryParse(seconds.Text.Trim(),System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture,out duration) || duration<2 || duration>30) {
            MessageBox.Show("请检查服务器地址、1–65535 端口，以及 2–30 秒的分段时长。"); return false;
        }
        var content=ReadConfig();
        string updated=content;
        updated=Set(updated,"addr","'"+hostname+"'");
        updated=Set(updated,"port","'"+pn+"'");
        updated=Set(updated,"mic_seg_duration",duration.ToString(System.Globalization.CultureInfo.InvariantCulture));
        updated=Set(updated,"mic_seg_overlap","0.5");
        updated=Set(updated,"live_output","True");
        updated=Set(updated,"enable_tray","False");
        updated=Set(updated,"udp_control","True");
        updated=Set(updated,"udp_control_addr","'127.0.0.1'");
        updated=Set(updated,"llm_enabled","False");
        if(capsHotkey.IsChecked==false)
            updated=Regex.Replace(updated,@"(?s)('key':\s*'caps_lock'.{0,180}?'enabled':\s*)True","$1False");
        else
            updated=Regex.Replace(updated,@"(?s)('key':\s*'caps_lock'.{0,180}?'enabled':\s*)False","$1True");
        if (updated!=content) {
            File.Copy(Config,Config+".bak",true);
            File.WriteAllText(Config,updated,new UTF8Encoding(false));
        }
        if(restart) { RestartBackend(); MessageBox.Show("已保存设置并重连。"); }
        return true;
    }

    static Process FindBackend() {
        foreach(var p in Process.GetProcessesByName("start_client")) {
            try { if(String.Equals(Path.GetFullPath(p.MainModule.FileName),Path.GetFullPath(Exe),StringComparison.OrdinalIgnoreCase)) return p; }
            catch{} p.Dispose();
        }
        return null;
    }
    static void StartBackend() {
        var existing=FindBackend();
        if(existing!=null) { backend=existing; ownsBackend=true; status.Text="已接入正在运行的客户端"; return; }
        logPosition=0; partialLine="";
        var psi=new ProcessStartInfo(Exe) { WorkingDirectory=Dir, UseShellExecute=false,
            CreateNoWindow=true, WindowStyle=ProcessWindowStyle.Hidden,
            RedirectStandardOutput=true, RedirectStandardError=true };
        backend=Process.Start(psi); ownsBackend=true;
        backend.OutputDataReceived+=(s,e)=>{};
        backend.ErrorDataReceived+=(s,e)=>{ if(e.Data!=null) main.Dispatcher.BeginInvoke(new Action(()=>AppendLog("错误: "+e.Data))); };
        backend.BeginOutputReadLine(); backend.BeginErrorReadLine();
    }
    static void RestartBackend() {
        if(backend!=null && !backend.HasExited) {
            if(!ownsBackend) { MessageBox.Show("已有其他程序启动的客户端。请先退出旧客户端，再重试。"); return; }
            backend.Kill(); backend.WaitForExit(5000);
        }
        backend=null; ownsBackend=false; connected=false; recording=false; processing=false;
        StartBackend();
    }
    static void SendControl(string command) {
        using(var udp=new UdpClient()) {
            var bytes=Encoding.ASCII.GetBytes(command);
            udp.Send(bytes,bytes.Length,new IPEndPoint(IPAddress.Loopback,6018));
        }
    }
    static void ToggleRecording() {
        if(backend==null || backend.HasExited || !connected) { MessageBox.Show("客户端尚未连接服务器。"); return; }
        SendControl(recording?"STOP":"START");
    }
    static void ReadLog() {
        if(!File.Exists(Log)) return;
        try {
            using(var f=new FileStream(Log,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete)) {
                if(f.Length<logPosition){logPosition=0;partialLine="";connected=false;}
                if(f.Length==logPosition) return;
                f.Seek(logPosition,SeekOrigin.Begin);
                using(var reader=new StreamReader(f,Encoding.UTF8,true,4096,true)) {
                    partialLine+=reader.ReadToEnd();logPosition=f.Position;
                }
            }
            var lines=Regex.Split(partialLine,@"\r?\n");
            partialLine=lines[lines.Length-1];
            for(int i=0;i<lines.Length-1;i++) ProcessLine(lines[i]);
        }catch(IOException){}
    }
    static void ProcessLine(string line) {
        if(line.Contains("WebSocket 建立成功")) connected=true;
        if(line.Contains("WebSocket") && (line.Contains("断开")||line.Contains("关闭")||line.Contains("失败"))) connected=false;
        if(line.Contains("触发：开始录音")) { recording=true; processing=false; started=DateTime.Now; lastText=""; }
        if(line.Contains("释放：完成录音")) { recording=false; processing=true; }
        int interim=line.IndexOf("实时识别片段:",StringComparison.Ordinal);
        if(interim>=0) lastText=line.Substring(interim+"实时识别片段:".Length).Trim();
        int final=line.IndexOf("收到最终识别结果:",StringComparison.Ordinal);
        if(final>=0) {lastText=line.Substring(final+9).Trim();processing=false;}
        if(line.Contains("实时输入已暂停") || line.Contains("最终文字未输入")) AppendLog(line);
        else if(line.Contains("触发：开始录音")||line.Contains("释放：完成录音")||interim>=0||final>=0||
            line.Contains("WebSocket 建立成功")||line.Contains("ERROR")) AppendLog(line);
    }
    static void AppendLog(string line) {
        logBox.AppendText(line+Environment.NewLine);
        if(logBox.Text.Length>24000) logBox.Text=logBox.Text.Substring(logBox.Text.Length-16000);
        logBox.ScrollToEnd();
    }
    static void UpdateDisplay() {
        bool alive=backend!=null && !backend.HasExited;
        string state=!alive?"客户端未运行":recording?"正在录音 "+(DateTime.Now-started).ToString(@"mm\:ss"):
            processing?"正在识别":connected?"已连接 · 待机":"正在连接服务器";
        status.Text=state+"   "+host.Text+":"+port.Text;
        floatStatus.Text=state;
        floatDetail.Text=recording?"边说边输入当前应用":lastText.Length>0?lastText:host.Text+":"+port.Text;
        transcript.Text=lastText.Length>0?lastText:"等待录音";
        mainRecord.Content=floatRecord.Content=recording?"结束录音":"开始录音";
        mainRecord.Background=floatRecord.Background=B(recording?"#D45161":"#3B70E6");
        mainRecord.IsEnabled=floatRecord.IsEnabled=alive && connected;
        tray.Text=recording?"CapsWriter · 正在录音":"CapsWriter · "+(connected?"已连接":"未连接");
    }
    static void Exit() {
        if(exiting) return; exiting=true;
        if(tick!=null) tick.Stop();
        if(tray!=null){tray.Visible=false;tray.Dispose();tray=null;}
        if(backend!=null){try{if(ownsBackend&&!backend.HasExited){backend.Kill();backend.WaitForExit(3000);}}catch{}backend.Dispose();backend=null;}
        if(floatWindow!=null) floatWindow.Close();
        if(main!=null) main.Close();
    }
}
