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
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;
using Forms = System.Windows.Forms;
using Path = System.IO.Path;

internal static partial class Desktop {
    [System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint="GetWindowLongW")]
    static extern int GetWindowLong(IntPtr h, int index);
    [System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint="SetWindowLongW")]
    static extern int SetWindowLong(IntPtr h, int index, int value);
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode)]
    static extern IntPtr FindWindow(string className, string windowName);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool ShowWindow(IntPtr h, int command);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool SetForegroundWindow(IntPtr h);

    static readonly string Dir = AppDomain.CurrentDomain.BaseDirectory;
    static readonly string Exe = Path.Combine(Dir, "start_client.exe");
    static readonly string Config = Path.Combine(Dir, "config_client.py");
    static readonly string Log = Path.Combine(Dir, "logs", "client_latest.log");
    static Mutex single;
    static Window main, floatWindow;
    static Forms.NotifyIcon tray;
    static Process backend;
    static DispatcherTimer tick;
    static TextBox host, port, seconds, contextWords, logBox, urlField;
    static PasswordBox apiKeyField;
    static TextBlock status, transcript, floatStatus, floatDetail, heroHint, sideStatus, apiKeyHint, serverHint;
    static UIElement lanFields, remoteFields;
    static Button lanModeButton, remoteModeButton;
    static Ellipse sideDot, floatConnectionDot;
    static Button mainRecord, floatRecord;
    static CheckBox showFloat;
    static bool connected, recording, exiting, ownsBackend, remoteMode;
    // 启动回放分离：GUI 启动/接入时记录日志文件的字节末尾作为历史边界；
    // 边界之前只展示不塑形，之后全部实时（不用“首批全历史”启发式）。
    // snapshotSynced：UDP STATE 初始只读快照是否已取得（未取得则 tick 重试）。
    static bool snapshotSynced;
    static long replayBoundary;
    // 启动回放分离：GUI 启动时先读到的既有日志是历史（不塑形实时状态）；
    // 接入后用 UDP STATE 快照同步真实录音/连接状态
    // 识别等待 = 真正未完成的识别任务集合（客户端“任务生命周期”事件驱动，
    // task_id 两端一致）。提交入集；完成/发送失败移除；断线、取消、后端
    // 退出清空。多段识别各自独立，不会因单个 final 提前熄灯或整体卡住。
    static readonly System.Collections.Generic.HashSet<string> pendingTasks
        =new System.Collections.Generic.HashSet<string>();
    static bool processing { get { return pendingTasks.Count>0; } }
    // 主窗口双 Tab（语音输入/设置）与识别等待状态灯
    static Button voiceTabButton, settingsTabButton;
    static UIElement voicePage, settingsPage;
    static Ellipse mainStateDot;
    static System.Windows.Shapes.Path mainSpinner;
    static RotateTransform mainSpinnerRotate;
    static DispatcherTimer stateAnimTick;
    static double stateAnimPhase;
    static bool mainLoaded;                    // 启动装配完成标志：快捷键自动应用仅在装配完成后生效
    static string lastText = "";
    static DateTime started;
    static long logPosition;
    static string partialLine = "";

    [STAThread]
    static void Main() {
        bool first;
        single = new Mutex(true, @"Local\CapsWriterUnifiedDesktop", out first);
        if (!first) {
            var existing=FindWindow(null,"CapsWriter · 语音输入");
            if(existing!=IntPtr.Zero) { ShowWindow(existing,9); SetForegroundWindow(existing); }
            else MessageBox.Show("CapsWriter 已在运行。请从托盘打开主界面。");
            return;
        }
        try {
            if (!File.Exists(Exe) || !File.Exists(Config))
                throw new FileNotFoundException("请把 CapsWriterDesktop.exe 放在 start_client.exe 和 config_client.py 旁边。");
            var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            LoadUiPrefs();
            BuildMain();
            BuildFloat();
            BuildTray();
            LoadSettings();
            SaveSettings(false);
            StartBackend();
            main.Show();
            if (showFloat.IsChecked == true) floatWindow.Show();
            CheckLastUpdateResult();
            tick = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
            tick.Tick += (s,e) => {
                ReadLog();
                // 初始只读快照未同步（UDP 未就绪）时的非阻塞重试：短超时、不拖慢 400ms 节拍
                if(!snapshotSynced && backend!=null && !backend.HasExited) {
                    var r=QueryBackend("STATE",80);
                    if(r!=null) ApplyStateReply(r);
                }
                UpdateDisplay();
            };
            tick.Start();
            ScheduleUpdateAutoCheck();
            app.Run();
        } catch (Exception ex) {
            MessageBox.Show(ex.ToString(), "CapsWriter 启动失败");
        } finally {
            if(!Exit()) MessageBox.Show("客户端进程未被终止：录音停止/输出恢复未确认。请稍后手动检查客户端。");
            single.ReleaseMutex();
            single.Dispose();
        }
    }

    static Brush B(string hex) { return (Brush)new BrushConverter().ConvertFromString(hex); }
    // 控件交互态：悬停/按下用半透明遮罩层，禁用整体降不透明度——
    // 不改动各按钮自己的主题背景色，明暗主题通用。
    // 结构：Border(Root) 唯一子级是 Grid，Grid 内叠 ContentPresenter 与
    // Shade 遮罩（Border 只能有一个 Child，遮罩必须放进容器层）。
    static ControlTemplate ButtonTemplate(double radius) {
        var border=new FrameworkElementFactory(typeof(Border));
        border.Name="Root";
        border.SetValue(Border.CornerRadiusProperty,new CornerRadius(radius));
        border.SetBinding(Border.BackgroundProperty,new System.Windows.Data.Binding("Background") {
            RelativeSource=new System.Windows.Data.RelativeSource(System.Windows.Data.RelativeSourceMode.TemplatedParent) });
        var layer=new FrameworkElementFactory(typeof(Grid));
        var content=new FrameworkElementFactory(typeof(ContentPresenter));
        content.SetBinding(FrameworkElement.HorizontalAlignmentProperty,
            new System.Windows.Data.Binding("HorizontalContentAlignment") {
                RelativeSource=new System.Windows.Data.RelativeSource(System.Windows.Data.RelativeSourceMode.TemplatedParent) });
        content.SetBinding(FrameworkElement.VerticalAlignmentProperty,
            new System.Windows.Data.Binding("VerticalContentAlignment") {
                RelativeSource=new System.Windows.Data.RelativeSource(System.Windows.Data.RelativeSourceMode.TemplatedParent) });
        layer.AppendChild(content);
        var shade=new FrameworkElementFactory(typeof(Border));
        shade.Name="Shade";
        shade.SetValue(Border.CornerRadiusProperty,new CornerRadius(radius));
        shade.SetValue(Border.BackgroundProperty,B("#000000"));
        shade.SetValue(Border.OpacityProperty,0.0);
        shade.SetValue(UIElement.IsHitTestVisibleProperty,false);
        layer.AppendChild(shade);
        border.AppendChild(layer);
        var template=new ControlTemplate(typeof(Button)) { VisualTree=border };
        var hover=new Trigger { Property=UIElement.IsMouseOverProperty, Value=true };
        hover.Setters.Add(new Setter(Border.OpacityProperty,0.06,"Shade"));
        var pressed=new Trigger { Property=System.Windows.Controls.Primitives.ButtonBase.IsPressedProperty, Value=true };
        pressed.Setters.Add(new Setter(Border.OpacityProperty,0.13,"Shade"));
        var disabled=new Trigger { Property=UIElement.IsEnabledProperty, Value=false };
        disabled.Setters.Add(new Setter(UIElement.OpacityProperty,0.45,"Root"));
        template.Triggers.Add(hover);template.Triggers.Add(pressed);template.Triggers.Add(disabled);
        return template;
    }
    static Button Btn(string value,string bg="#3F6AC4",string fg="#FFFFFF",double radius=10) {
        return new Button { Content=value, Height=40, Padding=new Thickness(16,0,16,0),
            Background=B(bg), Foreground=B(fg), BorderThickness=new Thickness(0),
            FontSize=13, FontWeight=FontWeights.SemiBold, Cursor=Cursors.Hand,
            Template=ButtonTemplate(radius) };
    }
    static TextBox Field() {
        return new TextBox { Height=42, FontSize=14, Foreground=T("#202226","#F0F1EF"),
            Background=T("#FBFBFA","#232529"), BorderBrush=T("#D8DAD6","#484B50"), BorderThickness=new Thickness(1),
            Padding=new Thickness(11,9,11,7), VerticalContentAlignment=VerticalAlignment.Center };
    }
    static CheckBox Switch(string title) {
        var toggle=new CheckBox { Content=title, Foreground=T("#33363A","#E7E8E6"), FontSize=13,
            VerticalAlignment=VerticalAlignment.Center, Cursor=Cursors.Hand };
        var root=new FrameworkElementFactory(typeof(StackPanel));
        root.SetValue(StackPanel.OrientationProperty,Orientation.Horizontal);
        var track=new FrameworkElementFactory(typeof(Border));
        track.Name="Track";
        track.SetValue(Border.WidthProperty,36.0);
        track.SetValue(Border.HeightProperty,21.0);
        track.SetValue(Border.CornerRadiusProperty,new CornerRadius(11));
        track.SetValue(Border.BackgroundProperty,T("#C9CCD0","#575B5F"));
        track.SetValue(Border.MarginProperty,new Thickness(0,0,10,0));
        var thumb=new FrameworkElementFactory(typeof(Ellipse));
        thumb.Name="Thumb";
        thumb.SetValue(FrameworkElement.WidthProperty,15.0);
        thumb.SetValue(FrameworkElement.HeightProperty,15.0);
        thumb.SetValue(FrameworkElement.MarginProperty,new Thickness(3,0,3,0));
        thumb.SetValue(FrameworkElement.HorizontalAlignmentProperty,HorizontalAlignment.Left);
        thumb.SetValue(Shape.FillProperty,T("#FFFFFF","#161719"));
        track.AppendChild(thumb);
        root.AppendChild(track);
        var caption=new FrameworkElementFactory(typeof(ContentPresenter));
        caption.SetValue(FrameworkElement.VerticalAlignmentProperty,VerticalAlignment.Center);
        root.AppendChild(caption);
        var template=new ControlTemplate(typeof(CheckBox)) { VisualTree=root };
        var on=new Trigger { Property=System.Windows.Controls.Primitives.ToggleButton.IsCheckedProperty, Value=true };
        on.Setters.Add(new Setter(Border.BackgroundProperty,T("#26282B","#EDEEEC"),"Track"));
        on.Setters.Add(new Setter(FrameworkElement.HorizontalAlignmentProperty,HorizontalAlignment.Right,"Thumb"));
        template.Triggers.Add(on);
        toggle.Template=template;
        return toggle;
    }
    static void BuildMain() {
        main=new Window { Title="CapsWriter · 语音输入", Width=760,Height=680,
            MinWidth=640,MinHeight=560,WindowStartupLocation=WindowStartupLocation.CenterScreen,
            WindowStyle=WindowStyle.None,ResizeMode=ResizeMode.CanResize,
            Background=T("#F6F7F5","#141517"),FontFamily=new FontFamily("Microsoft YaHei UI") };
        string iconPath=Path.Combine(Dir,"assets","icon.ico");
        if(File.Exists(iconPath)) main.Icon=System.Windows.Media.Imaging.BitmapFrame.Create(new Uri(iconPath));
        System.Windows.Shell.WindowChrome.SetWindowChrome(main,new System.Windows.Shell.WindowChrome {
            CaptionHeight=64,ResizeBorderThickness=new Thickness(6),GlassFrameThickness=new Thickness(0),
            CornerRadius=new CornerRadius(0),UseAeroCaptionButtons=false });
        main.Closing+=(s,e)=>{if(!exiting){e.Cancel=true;main.Hide();}};
        var root=new Grid { Background=T("#F6F7F5","#141517") };
        root.RowDefinitions.Add(new RowDefinition { Height=new GridLength(64) });
        root.RowDefinitions.Add(new RowDefinition { Height=GridLength.Auto });
        root.RowDefinitions.Add(new RowDefinition { Height=new GridLength(1,GridUnitType.Star) });

        var top=new Border { Background=T("#FFFFFF","#1D1F22"),
            BorderBrush=T("#E6E7E4","#35383C"),BorderThickness=new Thickness(0,0,0,1) };
        var topGrid=new Grid { Margin=new Thickness(22,0,14,0) };
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        var brand=new StackPanel { Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center };
        brand.Children.Add(AppMark(32));
        var brandText=Text("CapsWriter",15,"#1D1F22","#F1F2F0",true);
        brandText.Margin=new Thickness(11,0,0,0);brand.Children.Add(brandText);
        topGrid.Children.Add(brand);
        var badge=new Border { Background=T("#F0F1EE","#2C2F32"),
            CornerRadius=new CornerRadius(12),Padding=new Thickness(10,5,11,5),
            VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(0,0,11,0) };
        var badgeRow=new StackPanel { Orientation=Orientation.Horizontal };
        sideDot=new Ellipse { Width=7,Height=7,Fill=B("#A9ACAF"),
            VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(0,0,6,0) };
        badgeRow.Children.Add(sideDot);
        sideStatus=Text("连接中",11,"#5A5D62","#C7C9C8",true);badgeRow.Children.Add(sideStatus);
        badge.Child=badgeRow;Grid.SetColumn(badge,1);topGrid.Children.Add(badge);
        themeButton=ThemeButton("", "#F0F1EE","#2C2F32","#3A3D41","#E8E9E7",8);
        themeButton.Width=84;themeButton.Height=32;themeButton.FontSize=11;
        themeButton.Margin=new Thickness(0,0,8,0);
        themeButton.Click+=(s,e)=>{darkTheme=!darkTheme;ApplyTheme();SaveUiPrefs();};
        System.Windows.Shell.WindowChrome.SetIsHitTestVisibleInChrome(themeButton,true);
        Grid.SetColumn(themeButton,2);topGrid.Children.Add(themeButton);
        ApplyTheme();
        var windowButtons=new StackPanel { Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center };
        var minimize=ThemeButton("−","#FFFFFF","#1D1F22","#63666A","#C9CBC9",8);
        minimize.Width=34;minimize.Height=32;minimize.FontSize=18;
        minimize.Click+=(s,e)=>main.WindowState=WindowState.Minimized;
        System.Windows.Shell.WindowChrome.SetIsHitTestVisibleInChrome(minimize,true);
        windowButtons.Children.Add(minimize);
        var close=ThemeButton("×","#FFFFFF","#1D1F22","#63666A","#C9CBC9",8);
        close.Width=34;close.Height=32;close.FontSize=18;
        close.Click+=(s,e)=>main.Hide();
        System.Windows.Shell.WindowChrome.SetIsHitTestVisibleInChrome(close,true);
        windowButtons.Children.Add(close);
        Grid.SetColumn(windowButtons,3);topGrid.Children.Add(windowButtons);
        top.Child=topGrid;
        root.Children.Add(top);

        // ===== 顶部 Tab 条：语音输入 / 设置 =====
        var tabBar=new StackPanel { Orientation=Orientation.Horizontal,Margin=new Thickness(24,14,24,0) };
        voiceTabButton=ThemeButton("语音输入","#26282B","#EDEEEC","#FFFFFF","#1D1F22",9);
        voiceTabButton.Width=112;voiceTabButton.Height=38;
        voiceTabButton.Click+=(s,e)=>SelectMainTab(0);
        tabBar.Children.Add(voiceTabButton);
        settingsTabButton=ThemeButton("设置","#F0F1EE","#2E3134","#5A5D62","#C7C9C8",9);
        settingsTabButton.Width=112;settingsTabButton.Height=38;settingsTabButton.Margin=new Thickness(8,0,0,0);
        settingsTabButton.Click+=(s,e)=>SelectMainTab(1);
        tabBar.Children.Add(settingsTabButton);
        Grid.SetRow(tabBar,1);root.Children.Add(tabBar);

        voicePage=BuildVoicePage();
        settingsPage=BuildSettingsPage();
        var pages=new Grid();
        pages.Children.Add(voicePage);
        pages.Children.Add(settingsPage);
        settingsPage.Visibility=Visibility.Collapsed;
        Grid.SetRow(pages,2);root.Children.Add(pages);
        // 识别等待动画时钟：60ms 推进呼吸/旋转相位；启停由 UpdateDisplay 按真实状态切换
        stateAnimTick=new DispatcherTimer { Interval=TimeSpan.FromMilliseconds(60) };
        stateAnimTick.Tick+=(s,e)=>AnimateProcessingState();
        main.Content=root;
    }

    // 双 Tab 切换：语音输入页常驻录音/识别文字/运行记录；设置页集中服务器、
    // 软件更新、浮窗设置、识别与快捷键设置（后三者默认折叠）。
    static void SelectMainTab(int index) {
        if(voicePage!=null) voicePage.Visibility=index==0?Visibility.Visible:Visibility.Collapsed;
        if(settingsPage!=null) settingsPage.Visibility=index==1?Visibility.Visible:Visibility.Collapsed;
        if(voiceTabButton!=null) {
            voiceTabButton.Background=index==0?T("#26282B","#EDEEEC"):T("#F0F1EE","#2E3134");
            voiceTabButton.Foreground=index==0?T("#FFFFFF","#1D1F22"):T("#5A5D62","#C7C9C8");
        }
        if(settingsTabButton!=null) {
            settingsTabButton.Background=index==1?T("#26282B","#EDEEEC"):T("#F0F1EE","#2E3134");
            settingsTabButton.Foreground=index==1?T("#FFFFFF","#1D1F22"):T("#5A5D62","#C7C9C8");
        }
    }

    static ScrollViewer PageScroll(StackPanel body) {
        var scroll=new ScrollViewer { VerticalScrollBarVisibility=ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled };
        scroll.Resources[typeof(ScrollBar)]=SlimScrollBar();
        scroll.Content=body;
        return scroll;
    }

    static UIElement BuildVoicePage() {
        var body=new StackPanel { Margin=new Thickness(24,16,24,20) };
        body.Children.Add(Text("语音输入",23,"#1D1F22","#F1F2F0",true));
        var intro=Text("按住 CapsLock，或点击浮窗录音。文字会输入当前应用。",12,"#7A7E83","#A2A5A9");
        intro.Margin=new Thickness(0,4,0,17);body.Children.Add(intro);

        var recorder=new Grid();
        recorder.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        recorder.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        var recorderText=new StackPanel { VerticalAlignment=VerticalAlignment.Center };
        var statusRow=new StackPanel { Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center };
        mainStateDot=new Ellipse { Width=10,Height=10,Fill=B(IdleHex),VerticalAlignment=VerticalAlignment.Center,
            ToolTip="未连接" };
        statusRow.Children.Add(mainStateDot);
        status=Text("准备开始",19,"#1D1F22","#F1F2F0",true);status.Margin=new Thickness(10,0,0,0);
        statusRow.Children.Add(status);
        recorderText.Children.Add(statusRow);
        heroHint=Text("正在连接服务器…",11,"#7A7E83","#A2A5A9");
        heroHint.Margin=new Thickness(0,6,15,0);heroHint.TextWrapping=TextWrapping.Wrap;
        recorderText.Children.Add(heroHint);recorder.Children.Add(recorderText);
        mainRecord=ThemeButton("●  开始录音","#26282B","#EDEEEC","#FFFFFF","#1D1F22",10);
        mainRecord.Width=138;mainRecord.Height=42;mainRecord.VerticalAlignment=VerticalAlignment.Center;
        mainRecord.Click+=(s,e)=>{
            bool start=!recording;
            if(connected){ToggleRecording();if(start)main.WindowState=WindowState.Minimized;}
        };
        Grid.SetColumn(mainRecord,1);recorder.Children.Add(mainRecord);
        body.Children.Add(Surface(recorder,20));

        var modePanel=new StackPanel();
        modePanel.Children.Add(Text("输入方式",13,"#33363A","#E7E8E6",true));
        var modeRow=new StackPanel { Orientation=Orientation.Horizontal,
            Margin=new Thickness(0,10,0,0) };
        liveModeButton=ThemeButton("边说边写","#26282B","#EDEEEC","#FFFFFF","#1D1F22",9);
        liveModeButton.Width=116;liveModeButton.Height=36;
        liveModeButton.Click+=(s,e)=>SelectMode(true);
        modeRow.Children.Add(liveModeButton);
        batchModeButton=ThemeButton("录完再写","#F0F1EE","#2E3134","#5A5D62","#C7C9C8",9);
        batchModeButton.Width=116;batchModeButton.Height=36;
        batchModeButton.Margin=new Thickness(8,0,0,0);
        batchModeButton.Click+=(s,e)=>SelectMode(false);
        modeRow.Children.Add(batchModeButton);
        modePanel.Children.Add(modeRow);
        modeHint=Text("",11,"#7A7E83","#A2A5A9");
        modeHint.Margin=new Thickness(0,8,0,0);
        modePanel.Children.Add(modeHint);
        UpdateModeButtons();
        body.Children.Add(Surface(modePanel,19));

        var result=new StackPanel();
        result.Children.Add(Text("识别文字",13,"#33363A","#E7E8E6",true));
        transcript=Text("等待录音。你说的话会出现在这里。",14,"#5A5D62","#B9BBBD");
        transcript.TextWrapping=TextWrapping.Wrap;transcript.MaxHeight=80;
        transcript.Margin=new Thickness(0,10,0,2);result.Children.Add(transcript);
        body.Children.Add(Surface(result,19));

        // 运行记录：主界面常驻展开（非 Disclosure），可读区域 + 滚动
        var logPanel=new StackPanel();
        logPanel.Children.Add(SectionTitle("运行记录"));
        var logHint=Hint("客户端识别进度、空结果与错误实时显示在这里。");
        logHint.Margin=new Thickness(0,5,0,10);logHint.TextWrapping=TextWrapping.Wrap;
        logPanel.Children.Add(logHint);
        logBox=new TextBox { Height=176,Margin=new Thickness(0,0,0,0),IsReadOnly=true,
            TextWrapping=TextWrapping.Wrap,VerticalScrollBarVisibility=ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled,
            Background=T("#F5F6F4","#232529"),Foreground=T("#5C5F63","#C0C2C4"),
            BorderThickness=new Thickness(0),Padding=new Thickness(9),
            FontFamily=new FontFamily("Consolas"),FontSize=11 };
        logPanel.Children.Add(logBox);
        body.Children.Add(Surface(logPanel,19));
        return PageScroll(body);
    }

    static UIElement BuildSettingsPage() {
        var body=new StackPanel { Margin=new Thickness(24,16,24,20) };
        body.Children.Add(Text("设置",23,"#1D1F22","#F1F2F0",true));
        var intro=Text("服务器、更新、浮窗与识别快捷键集中在这里；分组默认折叠。",12,"#7A7E83","#A2A5A9");
        intro.Margin=new Thickness(0,4,0,17);intro.TextWrapping=TextWrapping.Wrap;body.Children.Add(intro);

        var server=new StackPanel();
        var serverHeading=new Grid();
        serverHeading.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        serverHeading.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        serverHeading.Children.Add(Text("服务器",13,"#33363A","#E7E8E6",true));
        var serverActions=new StackPanel { Orientation=Orientation.Horizontal };
        var restart=ThemeButton("重新连接","#F0F1EE","#2E3134","#5A5D62","#C7C9C8",8);
        restart.Width=88;restart.Height=30;restart.FontSize=11;
        restart.Click+=(s,e)=>{ if(RestartBackend()) status.Text="正在重新连接"; else status.Text="已取消重启：客户端恢复未确认"; };
        serverActions.Children.Add(restart);
        var test=ThemeButton("测试连接","#F0F1EE","#2E3134","#5A5D62","#C7C9C8",8);
        test.Width=88;test.Height=30;test.FontSize=11;test.Margin=new Thickness(8,0,0,0);
        test.Click+=(s,e)=>{
            if(!SaveSettings(false))return;
            if(RestartBackend()) status.Text="正在测试连接";
            else status.Text="已取消重连：客户端恢复未确认";
        };
        serverActions.Children.Add(test);
        Grid.SetColumn(serverActions,1);serverHeading.Children.Add(serverActions);
        server.Children.Add(serverHeading);

        var serverModeRow=new StackPanel { Orientation=Orientation.Horizontal,Margin=new Thickness(0,12,0,0) };
        lanModeButton=ThemeButton("局域网","#26282B","#EDEEEC","#FFFFFF","#1D1F22",9);
        lanModeButton.Width=92;lanModeButton.Height=34;
        lanModeButton.Click+=(s,e)=>SelectServerMode(false);
        serverModeRow.Children.Add(lanModeButton);
        remoteModeButton=ThemeButton("远程 wss","#F0F1EE","#2E3134","#5A5D62","#C7C9C8",9);
        remoteModeButton.Width=92;remoteModeButton.Height=34;remoteModeButton.Margin=new Thickness(8,0,0,0);
        remoteModeButton.Click+=(s,e)=>SelectServerMode(true);
        serverModeRow.Children.Add(remoteModeButton);
        server.Children.Add(serverModeRow);

        var lanPanel=new StackPanel();
        var fields=new Grid { Margin=new Thickness(0,12,0,0) };
        fields.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(2,GridUnitType.Star) });
        fields.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        fields.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        var addrCol=new StackPanel { Margin=new Thickness(0,0,12,0) };
        addrCol.Children.Add(Text("地址",11,"#7A7E83","#A2A5A9"));
        host=Field();host.Margin=new Thickness(0,5,0,0);addrCol.Children.Add(host);
        fields.Children.Add(addrCol);
        var portCol=new StackPanel { Margin=new Thickness(0,0,12,0) };
        portCol.Children.Add(Text("端口",11,"#7A7E83","#A2A5A9"));
        port=Field();port.Margin=new Thickness(0,5,0,0);portCol.Children.Add(port);
        Grid.SetColumn(portCol,1);fields.Children.Add(portCol);
        var save=ThemeButton("保存","#26282B","#EDEEEC","#FFFFFF","#1D1F22",9);
        save.Width=76;save.Height=42;save.VerticalAlignment=VerticalAlignment.Bottom;
        save.Click+=(s,e)=>SaveSettings(true);
        Grid.SetColumn(save,2);fields.Children.Add(save);
        lanPanel.Children.Add(fields);
        lanFields=lanPanel;
        server.Children.Add(lanFields);

        var remotePanel=new StackPanel();
        var remoteGrid=new Grid { Margin=new Thickness(0,12,0,0) };
        remoteGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(2,GridUnitType.Star) });
        remoteGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        remoteGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        var urlCol=new StackPanel { Margin=new Thickness(0,0,12,0) };
        urlCol.Children.Add(Text("远程地址",11,"#7A7E83","#A2A5A9"));
        urlField=Field();urlField.Margin=new Thickness(0,5,0,0);
        urlField.ToolTip="完整的 wss:// 服务地址，例如 wss://voice.example.com";
        urlCol.Children.Add(urlField);
        remoteGrid.Children.Add(urlCol);
        var keyCol=new StackPanel { Margin=new Thickness(0,0,12,0) };
        keyCol.Children.Add(Text("部署面板 API Key",11,"#7A7E83","#A2A5A9"));
        apiKeyField=new PasswordBox { Height=42,FontSize=14,Foreground=T("#202226","#F0F1EF"),
            Background=T("#FBFBFA","#232529"),BorderBrush=T("#D8DAD6","#484B50"),BorderThickness=new Thickness(1),
            Padding=new Thickness(11,9,11,7),VerticalContentAlignment=VerticalAlignment.Center };
        apiKeyField.Margin=new Thickness(0,5,0,0);
        keyCol.Children.Add(apiKeyField);
        Grid.SetColumn(keyCol,1);remoteGrid.Children.Add(keyCol);
        var saveRemote=ThemeButton("保存","#26282B","#EDEEEC","#FFFFFF","#1D1F22",9);
        saveRemote.Width=76;saveRemote.Height=42;saveRemote.VerticalAlignment=VerticalAlignment.Bottom;
        saveRemote.Click+=(s,e)=>SaveSettings(true);
        Grid.SetColumn(saveRemote,2);remoteGrid.Children.Add(saveRemote);
        remotePanel.Children.Add(remoteGrid);
        apiKeyHint=Text("Key 以当前 Windows 用户加密保存，不会写入配置或日志；便携包复制到新电脑需重新录入。",11,"#7A7E83","#A2A5A9");
        apiKeyHint.TextWrapping=TextWrapping.Wrap;apiKeyHint.Margin=new Thickness(0,8,0,0);
        remotePanel.Children.Add(apiKeyHint);
        remoteFields=remotePanel;
        remoteFields.Visibility=Visibility.Collapsed;
        server.Children.Add(remoteFields);

        serverHint=Text("",11,"#7A7E83","#A2A5A9");
        serverHint.TextWrapping=TextWrapping.Wrap;serverHint.Margin=new Thickness(0,8,0,0);
        server.Children.Add(serverHint);
        body.Children.Add(Surface(server,19));
        body.Children.Add(Disclosure("软件更新",UpdateSection()));

        var appearance=new StackPanel();
        showFloat=Switch("显示浮窗");showFloat.Margin=new Thickness(0,2,0,0);
        showFloat.Checked+=(s,e)=>{if(floatWindow!=null)floatWindow.Show();};
        showFloat.Unchecked+=(s,e)=>{if(floatWindow!=null)floatWindow.Hide();};
        appearance.Children.Add(showFloat);
        var glassRow=new Grid { Margin=new Thickness(0,13,0,0) };
        glassRow.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(90) });
        glassRow.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        glassRow.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(42) });
        glassRow.Children.Add(Text("背景浓度",11,"#7A7E83","#A2A5A9"));
        glassSlider=new Slider { Minimum=0,Maximum=100,Value=glassStrength,
            VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(0,0,12,0),
            TickFrequency=5,IsSnapToTickEnabled=true };
        glassSlider.ValueChanged+=(s,e)=>{glassStrength=glassSlider.Value;ApplyGlass();SaveUiPrefs();};
        Grid.SetColumn(glassSlider,1);glassRow.Children.Add(glassSlider);
        glassValue=Text("",11,"#5A5D62","#C7C9C8");
        Grid.SetColumn(glassValue,2);glassRow.Children.Add(glassValue);
        appearance.Children.Add(glassRow);
        body.Children.Add(Disclosure("浮窗设置",appearance));

        var advancedPanel=new StackPanel { Margin=new Thickness(0,2,0,2) };
        var secondsCol=new StackPanel { Width=145 };
        secondsCol.Children.Add(Text("停顿判定（秒）",11,"#7A7E83","#A2A5A9"));
        seconds=Field();seconds.Margin=new Thickness(0,5,0,0);secondsCol.Children.Add(seconds);
        pauseSetting=secondsCol;advancedPanel.Children.Add(secondsCol);
        advancedPanel.Children.Add(ShortcutSettingsPanel());
        var contextCol=new StackPanel { Margin=new Thickness(0,12,0,0) };
        contextCol.Children.Add(Text("识别提示词（可选）",11,"#7A7E83","#A2A5A9"));
        contextWords=Field();contextWords.Margin=new Thickness(0,5,0,0);
        contextWords.ToolTip="例如人名、产品名和专业术语。它会提示识别模型，但不会强制替换结果。";
        contextCol.Children.Add(contextWords);
        var contextHelp=Text("给模型提供易听错的词语线索，不会强制改写识别结果。",11,"#7A7E83","#A2A5A9");
        contextHelp.Margin=new Thickness(0,6,0,0);contextCol.Children.Add(contextHelp);
        advancedPanel.Children.Add(contextCol);
        body.Children.Add(Disclosure("识别与快捷键设置",advancedPanel));
        return PageScroll(body);
    }

    static void BuildFloat() {
        floatWindow=new Window { Width=floatCollapsed?116:320,Height=floatCollapsed?56:108,
            WindowStyle=WindowStyle.None,ResizeMode=ResizeMode.NoResize,
            AllowsTransparency=true,Background=Brushes.Transparent,
            Topmost=true,ShowInTaskbar=false,ShowActivated=false,
            FontFamily=new FontFamily("Microsoft YaHei UI") };
        var work=SystemParameters.WorkArea;
        floatWindow.Left=Double.IsNaN(floatLeft)?work.Right-floatWindow.Width-22:
            Math.Max(work.Left,Math.Min(floatLeft,work.Right-floatWindow.Width));
        floatWindow.Top=Double.IsNaN(floatTop)?work.Bottom-floatWindow.Height-22:
            Math.Max(work.Top,Math.Min(floatTop,work.Bottom-floatWindow.Height));
        floatWindow.SourceInitialized+=(s,e)=>{
            var h=new WindowInteropHelper(floatWindow).Handle;
            SetWindowLong(h,-20,GetWindowLong(h,-20)|0x08000000|0x80);
            ApplyGlass();
        };
        floatWindow.LocationChanged+=(s,e)=>{
            if(!layoutReady)return;
            floatLeft=floatWindow.Left;floatTop=floatWindow.Top;
        };
        floatWindow.Closing+=(s,e)=>{
            if(!exiting){e.Cancel=true;floatWindow.Hide();showFloat.IsChecked=false;}
        };
        floatOuter=new Border { BorderBrush=T("#55FFFFFF","#44FFFFFF"),
            BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(26),
            Padding=new Thickness(10,6,10,6) };
        var layers=new Grid();

        var expanded=new Grid();
        expanded.RowDefinitions.Add(new RowDefinition { Height=new GridLength(22) });
        expanded.RowDefinitions.Add(new RowDefinition { Height=new GridLength(1,GridUnitType.Star) });
        expanded.RowDefinitions.Add(new RowDefinition { Height=new GridLength(16) });
        var top=new Grid();
        top.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        top.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        var heading=new StackPanel { Orientation=Orientation.Horizontal };
        heading.Children.Add(AppMark(19));
        floatConnectionDot=ConnectionDot();
        floatConnectionDot.Margin=new Thickness(7,0,0,0);
        heading.Children.Add(floatConnectionDot);
        floatStatus=Text("",11,"#5C5E60","#D3D4D2",true);
        floatStatus.Margin=new Thickness(7,0,0,0);
        heading.Children.Add(floatStatus);top.Children.Add(heading);
        floatCollapseButton=WindowAction("collapse");
        floatCollapseButton.ToolTip="收起浮窗";
        floatCollapseButton.Click+=(s,e)=>SetFloatCollapsed(true);
        var close=WindowAction("close");
        close.ToolTip="隐藏浮窗";
        close.Click+=(s,e)=>{floatWindow.Hide();showFloat.IsChecked=false;};
        var topActions=WindowActions(floatCollapseButton,close);
        Grid.SetColumn(topActions,1);top.Children.Add(topActions);
        expanded.Children.Add(top);

        var recorder=new Grid();
        recorder.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        recorder.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        recorder.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        var leftWave=WaveBars(5);
        leftWave.HorizontalAlignment=HorizontalAlignment.Right;
        leftWave.Margin=new Thickness(0,0,13,0);
        recorder.Children.Add(leftWave);
        floatRecord=RecordButton(52);
        floatRecord.Click+=(s,e)=>ToggleRecording();
        Grid.SetColumn(floatRecord,1);recorder.Children.Add(floatRecord);
        var rightWave=WaveBars(5);
        rightWave.HorizontalAlignment=HorizontalAlignment.Left;
        rightWave.Margin=new Thickness(13,0,0,0);
        Grid.SetColumn(rightWave,2);recorder.Children.Add(rightWave);
        Grid.SetRow(recorder,1);expanded.Children.Add(recorder);
        floatDetail=Text("等待客户端连接",11,"#5C5E60","#C5C6C7");
        floatDetail.TextTrimming=TextTrimming.CharacterEllipsis;
        floatDetail.MaxWidth=292;
        floatDetail.TextAlignment=TextAlignment.Center;
        floatDetail.HorizontalAlignment=HorizontalAlignment.Center;
        Grid.SetRow(floatDetail,2);expanded.Children.Add(floatDetail);
        layers.Children.Add(expanded);expandedFloat=expanded;

        var compact=new StackPanel { Orientation=Orientation.Horizontal,
            HorizontalAlignment=HorizontalAlignment.Center,
            VerticalAlignment=VerticalAlignment.Center };
        compactRecord=RecordButton(40);
        ToolTipService.SetShowOnDisabled(compactRecord,true);
        compactRecord.Click+=(s,e)=>ToggleRecording();
        compact.Children.Add(compactRecord);
        var expandButton=WindowAction("expand");
        expandButton.ToolTip="展开浮窗";
        expandButton.Click+=(s,e)=>SetFloatCollapsed(false);
        var compactClose=WindowAction("close");
        compactClose.ToolTip="隐藏浮窗";
        compactClose.Click+=(s,e)=>{floatWindow.Hide();showFloat.IsChecked=false;};
        var compactActions=WindowActions(expandButton,compactClose);
        compactActions.Margin=new Thickness(6,0,0,0);
        compact.Children.Add(compactActions);
        compact.Visibility=floatCollapsed?Visibility.Visible:Visibility.Collapsed;
        expanded.Visibility=floatCollapsed?Visibility.Collapsed:Visibility.Visible;
        layers.Children.Add(compact);compactFloat=compact;
        waveTick=new DispatcherTimer { Interval=TimeSpan.FromMilliseconds(55) };
        waveTick.Tick+=(s,e)=>AnimateWave();
        floatOuter.Child=layers;
        floatOuter.MouseLeftButtonDown+=(s,e)=>{
            if(e.LeftButton!=MouseButtonState.Pressed)return;
            try{floatWindow.DragMove();}catch{}
            SaveUiPrefs();
        };
        floatWindow.Content=floatOuter;
        ApplyGlass();
        layoutReady=true;
    }

    sealed class TrayColors : Forms.ProfessionalColorTable {
        readonly System.Drawing.Color bg=System.Drawing.Color.FromArgb(24,25,27);
        readonly System.Drawing.Color hover=System.Drawing.Color.FromArgb(49,50,53);
        public override System.Drawing.Color ToolStripDropDownBackground { get { return bg; } }
        public override System.Drawing.Color MenuBorder { get { return bg; } }
        public override System.Drawing.Color MenuItemBorder { get { return hover; } }
        public override System.Drawing.Color MenuItemSelected { get { return hover; } }
        public override System.Drawing.Color MenuItemSelectedGradientBegin { get { return hover; } }
        public override System.Drawing.Color MenuItemSelectedGradientEnd { get { return hover; } }
        public override System.Drawing.Color ImageMarginGradientBegin { get { return bg; } }
        public override System.Drawing.Color ImageMarginGradientMiddle { get { return bg; } }
        public override System.Drawing.Color ImageMarginGradientEnd { get { return bg; } }
    }
    static void BuildTray() {
        var iconPath=Path.Combine(Dir,"assets","icon.ico");
        tray=new Forms.NotifyIcon { Visible=true, Text="CapsWriter 语音输入",
            Icon=File.Exists(iconPath)?new System.Drawing.Icon(iconPath):System.Drawing.SystemIcons.Information };
        var menu=new Forms.ContextMenuStrip { ShowImageMargin=false,
            BackColor=System.Drawing.Color.FromArgb(24,25,27),
            ForeColor=System.Drawing.Color.FromArgb(239,246,255),
            Font=new System.Drawing.Font("Microsoft YaHei UI",9.5f),
            Padding=new Forms.Padding(7,6,7,6) };
        menu.Renderer=new Forms.ToolStripProfessionalRenderer(new TrayColors());
        menu.Items.Add("打开主界面",null,(s,e)=>main.Dispatcher.Invoke(()=>{main.Show();main.Activate();}));
        menu.Items.Add("显示 / 隐藏浮窗",null,(s,e)=>main.Dispatcher.Invoke(()=>showFloat.IsChecked=showFloat.IsChecked!=true));
        menu.Items.Add("开始 / 停止录音",null,(s,e)=>main.Dispatcher.Invoke(()=>ToggleRecording()));
        menu.Items.Add("检查更新",null,(s,e)=>main.Dispatcher.Invoke(()=>{main.Show();main.Activate();CheckForUpdates(true);}));
        menu.Items.Add("退出",null,(s,e)=>main.Dispatcher.Invoke(()=>{
            if(Exit()) Application.Current.Shutdown();
            else MessageBox.Show("客户端录音停止/输出恢复未确认，已取消退出。请稍后重试。");
        }));
        foreach(Forms.ToolStripItem item in menu.Items) {
            item.ForeColor=System.Drawing.Color.FromArgb(239,246,255);
            item.Padding=new Forms.Padding(6,5,6,5);
        }
        tray.ContextMenuStrip=menu;
        tray.DoubleClick+=(s,e)=>main.Dispatcher.Invoke(()=>{main.Show();main.Activate();});
    }

    static string ReadConfig() { return File.ReadAllText(Config,Encoding.UTF8); }
    static string Value(string source,string key,string fallback) {
        // 仅用于无引号的数字/布尔 token；带引号的字符串一律走 TryReadStringLiteral
        var m=Regex.Match(source,@"(?m)^\s*"+Regex.Escape(key)+@"\s*=\s*(.+?)\s*(?:#.*)?$");
        return m.Success ? m.Groups[1].Value.Trim() : fallback;
    }
    // A02：严格解析 Python 字符串字面量——注释只在字符串外识别（字符串里的
    // # 不截断）、不用 Trim 引号集合（末尾转义单引号不被误剥）、转义按
    // Python 语义解码。解析失败返回 false。
    static bool TryReadStringLiteral(string source,string key,out string value) {
        value=null;
        if(source==null||key==null) return false;
        var m=Regex.Match(source,@"(?m)^\s*"+Regex.Escape(key)+@"\s*=\s*");
        if(!m.Success) return false;
        int i=m.Index+m.Length;
        if(i>=source.Length||(source[i]!='\''&&source[i]!='"')) return false;
        char quote=source[i++];
        var sb=new System.Text.StringBuilder();
        while(i<source.Length) {
            char c=source[i++];
            if(c=='\\') {
                if(i>=source.Length) return false;      // 字符串以反斜杠结尾：未闭合
                char e=source[i++];
                switch(e) {
                    case 'n': sb.Append('\n'); break;
                    case 't': sb.Append('\t'); break;
                    case 'r': sb.Append('\r'); break;
                    case '\\': sb.Append('\\'); break;
                    case '\'': sb.Append('\''); break;
                    case '"': sb.Append('"'); break;
                    default: sb.Append('\\').Append(e); break;
                }
                continue;
            }
            if(c==quote) { value=sb.ToString(); return true; }
            if(c=='\r'||c=='\n') return false;           // 引号未闭合就换行
            sb.Append(c);
        }
        return false;                                     // 字符串未闭合
    }
    // A02：Python 单引号字面量序列化（与 TryReadStringLiteral 互逆）
    static string EscapePy(string s) {
        return s.Replace("\\","\\\\").Replace("'","\\'");
    }
    // A02（第二轮预审反例）：addr/port 在配置里是带引号的 Python 字符串字面量，
    // 必须用严格解析器读取——Value() 会把引号一起返回，输入框变成
    // '127.0.0.1'，LAN 保存的输入校验必然失败。
    static string ReadConfigString(string source,string key,string fallback) {
        string v;
        return TryReadStringLiteral(source,key,out v)?v:fallback;
    }
    // A02：加载->编辑->保存链条的纯函数层（反射实测）。返回 LoadSettings
    // 实际填充输入框的值；SaveSettings 的 LAN 分支用的 host/port 就来自这里。
    static System.Collections.Generic.Dictionary<string,string> LoadConfigValues(string content) {
        var v=new System.Collections.Generic.Dictionary<string,string>();
        v["addr"]=ReadConfigString(content,"addr","127.0.0.1");
        v["port"]=ReadConfigString(content,"port","6016");
        v["server_url"]=ReadConfigString(content,"server_url","");
        string ps;
        v["pause_seconds"]=TryReadStringLiteralOrToken(content,"pause_seconds",out ps)?ps:"0.75";
        // pause_segmented 优先，缺失时回退 live_output（与 LoadSettings 语义一致）
        string seg;
        if (!TryReadStringLiteralOrToken(content,"pause_segmented",out seg)) {
            if (!TryReadStringLiteralOrToken(content,"live_output",out seg)) seg="True";
        }
        v["pause_segmented"]=seg;
        v["context"]=ReadConfigString(content,"context","");
        return v;
    }
    // token 读取：带引号字符串或裸 True/False/数字都接受（严格字面量优先）
    static bool TryReadStringLiteralOrToken(string source,string key,out string value) {
        if (TryReadStringLiteral(source,key,out value)) return true;
        var m=Regex.Match(source,@"(?m)^\s*"+Regex.Escape(key)+@"\s*=\s*(.+?)\s*(?:#.*)?$");
        if (m.Success) { value=m.Groups[1].Value.Trim(); return true; }
        value=""; return false;
    }
    static void LoadSettings() {
        var content=ReadConfig();
        var loaded=LoadConfigValues(content);
        host.Text=loaded["addr"];
        port.Text=loaded["port"];
        // R09/A02：配置里是转义过的 Python 字面量，严格解析还原真实地址
        string remoteUrl=loaded["server_url"];
        remoteMode=remoteUrl.StartsWith("ws://",StringComparison.OrdinalIgnoreCase)
            || remoteUrl.StartsWith("wss://",StringComparison.OrdinalIgnoreCase);
        urlField.Text=remoteMode?remoteUrl:"";
        LoadStoredApiKey();
        seconds.Text=loaded["pause_seconds"];
        liveMode=loaded["pause_segmented"]=="True";
        UpdateModeButtons();
        contextWords.Text=loaded["context"];
        double previous;
        if (!Double.TryParse(seconds.Text, System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out previous) ||
            previous<0.3 || previous>2.5) seconds.Text="0.75";
        showFloat.IsChecked=true;
        LoadShortcutSettings(content);
        UpdateServerModeButtons();
        // 启动装配完成：此后快捷键 UI 的变更才会触发自动保存/应用
        mainLoaded=true;
    }
    static void SelectServerMode(bool remote) {
        remoteMode=remote;
        UpdateServerModeButtons();
        SaveSettings(false);
    }
    static void UpdateServerModeButtons() {
        if (lanModeButton==null || remoteModeButton==null) return;
        lanModeButton.Background=remoteMode?T("#F0F1EE","#2E3134"):T("#26282B","#EDEEEC");
        lanModeButton.Foreground=remoteMode?T("#5A5D62","#C7C9C8"):T("#FFFFFF","#1D1F22");
        remoteModeButton.Background=remoteMode?T("#26282B","#EDEEEC"):T("#F0F1EE","#2E3134");
        remoteModeButton.Foreground=remoteMode?T("#FFFFFF","#1D1F22"):T("#5A5D62","#C7C9C8");
        if (lanFields!=null) lanFields.Visibility=remoteMode?Visibility.Collapsed:Visibility.Visible;
        if (remoteFields!=null) remoteFields.Visibility=remoteMode?Visibility.Visible:Visibility.Collapsed;
        if (serverHint!=null) serverHint.Text=remoteMode?
            "远程地址走加密 wss 通道，适合家庭网络之外；网关用部署面板 API Key 校验接入。":
            "局域网地址为明文 ws 连接，仅适合可信网络；是否开放直连由服务器端控制。";
        if (apiKeyHint!=null && apiKeyField!=null)
            apiKeyHint.Text=(apiKeyField.Tag==ApiKeyLegacyMarker)?
                "检测到旧版客户端令牌（已忽略）。远程接入需在下方重新录入部署面板 API Key。":
                (apiKeyField.Tag==ApiKeySavedMarker)?
                "已保存部署面板 API Key（以当前 Windows 用户加密存储）。更换 Key 时输入新值并保存。":
                "Key 以当前 Windows 用户加密保存，不会写入配置或日志；便携包复制到新电脑需重新录入。";
    }
    static string ServerDisplay() {
        if (remoteMode && !String.IsNullOrWhiteSpace(urlField.Text)) return urlField.Text.Trim();
        return host.Text.Trim()+":"+port.Text.Trim();
    }
    static string Set(string source,string key,string value) {
        string pattern=@"(?m)^(\s*"+Regex.Escape(key)+@"\s*=\s*).*$";
        if(Regex.IsMatch(source,pattern)) return Regex.Replace(source,pattern,m=>m.Groups[1].Value+value,RegexOptions.Multiline);
        return source.Replace("class ClientConfig:", "class ClientConfig:\r\n    "+key+" = "+value);
    }
    // A02：按目标模式生成整份配置（纯函数，反射实测）。LAN 模式的目标
    // server_url 就是空串——不读隐藏的远程地址字段。
    static string ApplyConfig(string content,bool remoteMode,string remoteUrl,string hostname,
                              ushort pn,double duration,bool liveMode,string prompt) {
        string updated=content;
        if (remoteMode) {
            // R09：URL 作为 Python 单引号字符串写入，反斜杠与单引号必须转义——
            // 否则 wss://…/O'Reilly 这类合法路径会生成无法编译的 config_client.py
            updated=Set(updated,"server_url","'"+EscapePy(remoteUrl)+"'");
        } else {
            updated=Set(updated,"server_url","''");
            updated=Set(updated,"addr","'"+EscapePy(hostname)+"'");
            updated=Set(updated,"port","'"+pn+"'");
        }
        updated=Set(updated,"mic_seg_duration","60");
        updated=Set(updated,"mic_seg_overlap",liveMode?"0":"4");
        updated=Set(updated,"pause_seconds",duration.ToString(System.Globalization.CultureInfo.InvariantCulture));
        updated=Set(updated,"pause_segmented",liveMode?"True":"False");
        updated=Set(updated,"context","'"+EscapePy(prompt)+"'");
        updated=Set(updated,"live_output",liveMode?"True":"False");
        updated=Set(updated,"enable_tray","False");
        updated=Set(updated,"udp_control","True");
        updated=Set(updated,"udp_control_addr","'127.0.0.1'");
        updated=Set(updated,"llm_enabled","False");
        return updated;
    }
    // A02：写入前回读校验——用严格解析器核对每个写入键，反序列化值必须与
    // 输入一致；不一致返回原因，null 表示整份可解析。LAN 模式的期望
    // server_url 是空串，不与隐藏远程字段比较。
    static string ValidateUpdate(string updated,bool remoteMode,string remoteUrl,string hostname,
                                 ushort pn,string prompt) {
        string su;
        if(!TryReadStringLiteral(updated,"server_url",out su)) return "server_url 不是可解析的字符串字面量";
        if(su!=(remoteMode?remoteUrl:"")) return "server_url 回读与目标不一致";
        if(!remoteMode) {
            string addr;
            if(!TryReadStringLiteral(updated,"addr",out addr)||addr!=hostname) return "addr 回读与输入不一致";
            string prt;
            if(!TryReadStringLiteral(updated,"port",out prt)||prt!=pn.ToString()) return "port 回读与输入不一致";
        }
        string ctx;
        if(!TryReadStringLiteral(updated,"context",out ctx)||ctx!=prompt) return "context 回读与输入不一致";
        string udp;
        if(!TryReadStringLiteral(updated,"udp_control_addr",out udp)||udp!="127.0.0.1") return "udp_control_addr 回读不一致";
        return null;
    }
    // A03/A02：远程地址输入校验（纯函数，反射实测）——返回错误信息，null=通过
    static string RemoteUrlValidationError(string remoteUrl) {
        if (remoteUrl.Length==0 || remoteUrl.Length>300 ||
            !Regex.IsMatch(remoteUrl,@"^wss://[A-Za-z0-9.\-]+(:\d{1,5})?(/[^\s]*)?$|^wss://\[[0-9A-Fa-f:.]+\](:\d{1,5})?(/[^\s]*)?$",
                RegexOptions.IgnoreCase)) {
            return "远程地址必须是完整的 wss:// 地址，例如 wss://voice.example.com。\r\n远程模式不接受明文 ws://（部署面板 Key 不能走明文链路）；局域网直连请切换到“局域网”模式。";
        }
        // R09：端口边界显式核对（\d{1,5} 会放过 99999 这类越界值）
        Match portMatch=Regex.Match(remoteUrl,@"^(?:wss://[^/:]+|wss://\[[^\]]+\]):(\d{1,5})(?:[/?#]|$)",RegexOptions.IgnoreCase);
        ushort pn;
        if (portMatch.Success && (!UInt16.TryParse(portMatch.Groups[1].Value,out pn) || pn==0)) {
            return "远程地址端口必须在 1–65535 之间。";
        }
        int schemeEnd=remoteUrl.IndexOf("://",StringComparison.OrdinalIgnoreCase)+3;
        int slashIdx=remoteUrl.IndexOf('/',schemeEnd);
        string authority=slashIdx<0?remoteUrl.Substring(schemeEnd):remoteUrl.Substring(schemeEnd,slashIdx-schemeEnd);
        if (authority.IndexOf('@')>=0) {
            return "远程地址不能携带用户信息（user:pass@），部署面板 API Key 请录入客户端凭据存储。";
        }
        // A03：fragment 与凭据查询参数一律拒绝——Key 只进本机 DPAPI 凭据存储。
        // 查询名必须先按规范解码（%xx、考虑大小写）再匹配，%61pi_key 等
        // 编码形式不能绕过；桌面与客户端的凭据名清单保持一致。
        if (remoteUrl.IndexOf('#')>=0) {
            return "远程地址不能携带 fragment（#）；部署面板 API Key 只保存在本机凭据存储。";
        }
        int queryIdx=remoteUrl.IndexOf('?');
        if (queryIdx>=0) {
            foreach (string rawPair in remoteUrl.Substring(queryIdx+1).Split('&')) {
                if (rawPair.Length==0) continue;
                string rawName=rawPair.Split('=')[0];
                string decoded=rawName;
                // 最多两轮 %xx 解码，防双重编码绕过；'+' 在查询串中是空格
                for (int pass=0; pass<2 && decoded.IndexOf('%')>=0; pass++) {
                    try { decoded=Uri.UnescapeDataString(decoded); } catch (ArgumentException) { break; }
                }
                decoded=decoded.Replace('+',' ').Trim().ToLowerInvariant();
                if (Regex.IsMatch(decoded,@"^(?:x[_-]?api[_-]?key|api[_-]?key|apikey|access[_-]?token|server[_-]?token|secret|password|passwd|token|auth|authorization)$")) {
                    return "远程地址查询参数不能携带凭据（"+decoded+"）；部署面板 API Key 请录入客户端凭据存储。";
                }
            }
        }
        return null;
    }
    static bool SaveSettings(bool restart) {
        EndShortcutCapture();
        if(!ValidateShortcutSettings())return false;
        string hostname=host.Text.Trim();
        ushort pn=0; double duration;
        string remoteUrl=urlField!=null?urlField.Text.Trim():"";
        if (remoteMode) {
            string urlError=RemoteUrlValidationError(remoteUrl);
            if (urlError!=null) { MessageBox.Show(urlError); return false; }
        } else if (hostname.Length==0 || hostname.Length>253 || !Regex.IsMatch(hostname,@"^[a-zA-Z0-9.:-]+$") ||
            !UInt16.TryParse(port.Text.Trim(),out pn) || pn==0) {
            MessageBox.Show("请检查服务器地址与 1–65535 端口。"); return false;
        }
        if (!Double.TryParse(seconds.Text.Trim(),System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture,out duration) || duration<0.3 || duration>2.5) {
            MessageBox.Show("停顿判定须在 0.3–2.5 秒之间。"); return false;
        }
        string prompt=contextWords.Text.Trim();
        if(prompt.Length>120 || prompt.IndexOfAny(new[]{'\r','\n'})>=0) {
            MessageBox.Show("识别提示词最多 120 字，不能包含换行。"); return false;
        }
        var content=ReadConfig();
        string updated=ApplyConfig(content,remoteMode,remoteUrl,hostname,pn,duration,liveMode,prompt);
        updated=ReplaceShortcutBlock(updated);
        // A02：严格回读校验，失败不写盘（保留原配置与 .bak 链路）
        string invalid=ValidateUpdate(updated,remoteMode,remoteUrl,hostname,pn,prompt);
        if (invalid!=null) {
            MessageBox.Show("配置回读校验失败（"+invalid+"），已放弃写入；原配置未改动。");
            return false;
        }
        if (updated!=content) {
            File.Copy(Config,Config+".bak",true);
            File.WriteAllText(Config,updated,new UTF8Encoding(false));
        }
        // Key 绝不写入配置文件，只进当前用户 DPAPI 凭据存储
        if (remoteMode && apiKeyField!=null && apiKeyField.Password.Length>0) {
            try { SaveStoredApiKey(apiKeyField.Password.Trim()); }
            catch (Exception ex) { MessageBox.Show("API Key 保存失败："+ex.Message); return false; }
            apiKeyField.Clear();
            UpdateServerModeButtons();
        }
        if(restart) {
            if(RestartBackend()) status.Text="设置已保存，正在重连";
            else status.Text="已保存，但重连被取消：客户端恢复未确认";
        }
        return true;
    }

    static string CredentialPath {
        get {
            string local=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            return Path.Combine(local,"CapsWriterOffline","credentials.json");
        }
    }
    static void LoadStoredApiKey() {
        apiKeyField.Password="";
        apiKeyField.Tag=null;
        try {
            string file=CredentialPath;
            if (!File.Exists(file)) return;
            var json=File.ReadAllText(file,Encoding.UTF8);
            if (!json.Contains("\"protected\": \"dpapi\"") && !json.Contains("\"protected\":\"dpapi\"")) return;
            var m=Regex.Match(json,"\"blob\"\\s*:\\s*\"([A-Za-z0-9+/=]+)\"");
            if (!m.Success) return;
            var blob=Convert.FromBase64String(m.Groups[1].Value);
            var plain=System.Security.Cryptography.ProtectedData.Unprotect(blob,null,
                System.Security.Cryptography.DataProtectionScope.CurrentUser);
            var inner=Encoding.UTF8.GetString(plain);
            // 新键 api_key = 部署面板 Key；旧键 server_token 是已废弃的 cw. 客户端令牌，
            // 绝不能当作面板 Key 发送，只提示重新录入。
            if (Regex.IsMatch(inner,"\"api_key\"\\s*:\\s*\"[^\"]+\"")) {
                apiKeyField.Tag=ApiKeySavedMarker;
            } else if (Regex.IsMatch(inner,"\"server_token\"\\s*:\\s*\"[^\"]+\"")) {
                apiKeyField.Tag=ApiKeyLegacyMarker;
            }
        } catch (Exception) { apiKeyField.Tag=null; }
        UpdateServerModeButtons();
    }
    static void SaveStoredApiKey(string apiKey) {
        var payload="{\"api_key\":\""+apiKey.Replace("\\","\\\\").Replace("\"","\\\"")+"\"}";
        var blob=System.Security.Cryptography.ProtectedData.Protect(Encoding.UTF8.GetBytes(payload),null,
            System.Security.Cryptography.DataProtectionScope.CurrentUser);
        string json="{\n  \"protected\": \"dpapi\",\n  \"blob\": \""+Convert.ToBase64String(blob)+"\"\n}";
        string file=CredentialPath;
        Directory.CreateDirectory(Path.GetDirectoryName(file));
        File.WriteAllText(file,json,new UTF8Encoding(false));
        apiKeyField.Tag=ApiKeySavedMarker;
    }
    static readonly object ApiKeySavedMarker=new object();
    static readonly object ApiKeyLegacyMarker=new object();

    static Process FindBackend() {
        foreach(var p in Process.GetProcessesByName("start_client")) {
            try { if(String.Equals(Path.GetFullPath(p.MainModule.FileName),Path.GetFullPath(Exe),StringComparison.OrdinalIgnoreCase)) return p; }
            catch{} p.Dispose();
        }
        return null;
    }
    // STATE 快照：STATE|recording=<0/1>|connected=<0/1>|started=<unix秒>
    // （只读命令）。返回 null=无回执（旧客户端无此命令），调用方回退日志驱动。
    static string QueryBackend(string command, int timeoutMs) {
        try {
            using(var udp=new UdpClient()) {
                udp.Client.ReceiveTimeout=timeoutMs;
                var bytes=Encoding.ASCII.GetBytes(command);
                udp.Send(bytes,bytes.Length,new IPEndPoint(IPAddress.Loopback,6018));
                var remote=new IPEndPoint(IPAddress.Loopback,0);
                byte[] data;
                try { data=udp.Receive(ref remote); }
                catch(System.Net.Sockets.SocketException) { return null; }
                if(remote.Port!=6018 || !IPAddress.IsLoopback(remote.Address)) return null;
                return Encoding.ASCII.GetString(data);
            }
        } catch { return null; }
    }
    // 把 STATE 回执落到真实 UI 状态（非伪造、非超时）：录音与连接以客户端
    // 当前事实为准；不清在途识别任务（已提交的最终结果仍会返回，idle 快照
    // 不得误清旧 pending）。started 为录音开始的 Unix 秒，校正计时显示。
    static void ApplyStateReply(string reply) {
        if(reply==null || !reply.StartsWith("STATE|")) return;
        bool rec=recording; long startedEpoch=0;
        var parts=reply.Split('|');
        foreach(var p in parts) {
            if(p.StartsWith("recording=")) rec=p=="recording=1";
            else if(p.StartsWith("connected=")) connected=p=="connected=1";
            else if(p.StartsWith("started=")) long.TryParse(p.Substring(8),
                System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture,out startedEpoch);
        }
        recording=rec;
        if(rec && startedEpoch>0)
            started=new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc).AddSeconds(startedEpoch).ToLocalTime();
        snapshotSynced=true;
    }
    // 启动/接入：全新快照（此前无可保留的旧状态）；UDP 未就绪时由 tick 重试
    static void SyncBackendSnapshot() {
        // 只读快照同步 recording/connected；不清旧在途识别任务
        //（快照 idle 只说明此刻未录音，已提交任务的最终结果仍在途中）
        var reply=QueryBackend("STATE",900);
        if(reply!=null) ApplyStateReply(reply);
    }
    // 启动/接入时刻的日志字节末尾：之前的行=历史（只展示不塑形）
    static long ReplayBoundaryBytes() {
        try {
            using(var f=new FileStream(Log,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
                return f.Length;
        } catch { return 0; }
    }
    static void StartBackend() {
        var existing=FindBackend();
        // 先定格旧日志边界与偏移，再启动/接入：新进程启动后写入的首批事件
        // 不得划入历史（边界必须是启动前旧日志的字节末尾）
        logPosition=0; partialLine=""; snapshotSynced=false;
        replayBoundary=ReplayBoundaryBytes();
        if(existing!=null) { backend=existing; ownsBackend=true; status.Text="已接入正在运行的客户端"; }
        else {
            var psi=new ProcessStartInfo(Exe) { WorkingDirectory=Dir, UseShellExecute=false,
                CreateNoWindow=true, WindowStyle=ProcessWindowStyle.Hidden,
                RedirectStandardOutput=true, RedirectStandardError=true };
            backend=Process.Start(psi); ownsBackend=true;
            backend.OutputDataReceived+=(s,e)=>{};
            backend.ErrorDataReceived+=(s,e)=>{ if(e.Data!=null) main.Dispatcher.BeginInvoke(new Action(()=>AppendLog("错误: "+e.Data))); };
            backend.BeginOutputReadLine(); backend.BeginErrorReadLine();
        }
        SyncBackendSnapshot();
    }
    static bool RestartBackend() {
        if(!TryPrepareBackendShutdown()) {
            MessageBox.Show("无法确认客户端已停止录音并恢复输出静音，已取消本次重启。请检查客户端进程后重试。");
            return false;
        }
        if(backend!=null && !backend.HasExited) {
            if(!ownsBackend) { MessageBox.Show("已有其他程序启动的客户端。请先退出旧客户端，再重试。"); return false; }
            backend.Kill(); backend.WaitForExit(5000);
        }
        backend=null; ownsBackend=false; connected=false; recording=false; pendingTasks.Clear();
        StartBackend();
        return true;
    }
    // 桌面端终止/重启客户端前的专用握手（普通 STOP 不受影响）：
    // - 无条件发送 PREPARE_SHUTDOWN|<nonce>（界面录音标志来自日志可能滞后）；
    // - 客户端先落下关闭闩锁（此后任何快捷键/UDP START 都无法重新静音）、
    //   同步结束全部录音并在完成输出静音恢复后才回执；
    // - 仅当回执 SHUTDOWN_READY|<pid>|<nonce>、来源为 127.0.0.1:6018、
    //   PID 与本进程记录的 backend 一致时才算确认；
    //   SHUTDOWN_RESTORE_FAILED|... 表示客户端仍有设备恢复失败；
    // - 未确认前绝不 Kill：超时/失败返回 false，调用方必须放弃终止。
    static bool TryPrepareBackendShutdown() {
        var proc=backend;
        if(proc==null || proc.HasExited || !ownsBackend) return true;   // 没有受控进程需要终止
        try {
            using(var udp=new UdpClient()) {
                udp.Client.ReceiveTimeout=300;
                var server=new IPEndPoint(IPAddress.Loopback,6018);
                var deadline=Environment.TickCount+5000;
                var nonce=Environment.TickCount.ToString("X8");
                while(Environment.TickCount<deadline) {
                    var bytes=Encoding.ASCII.GetBytes("PREPARE_SHUTDOWN|"+nonce);
                    try { udp.Send(bytes,bytes.Length,server); } catch {}
                    while(Environment.TickCount<deadline) {
                        var remote=new IPEndPoint(IPAddress.Loopback,0);
                        byte[] data;
                        try { data=udp.Receive(ref remote); }
                        catch(System.Net.Sockets.SocketException) { break; }   // 接收超时→重发
                        if(remote.Port!=6018 || !IPAddress.IsLoopback(remote.Address)) continue;
                        var text=Encoding.ASCII.GetString(data);
                        if(!text.StartsWith("SHUTDOWN_READY|") && !text.StartsWith("SHUTDOWN_RESTORE_FAILED|")) continue;
                        var parts=text.Split('|');
                        int pid; if(parts.Length<3 || !int.TryParse(parts[1],out pid) || pid!=proc.Id || parts[2]!=nonce) continue;
                        return text.StartsWith("SHUTDOWN_READY|");
                    }
                }
            }
        } catch {}
        return false;   // 无确认：调用方必须放弃终止
    }
    static void SendControl(string command) {
        using(var udp=new UdpClient()) {
            var bytes=Encoding.ASCII.GetBytes(command);
            udp.Send(bytes,bytes.Length,new IPEndPoint(IPAddress.Loopback,6018));
        }
    }
    static void ToggleRecording() {
        if(backend==null || backend.HasExited || !connected) { MessageBox.Show("客户端尚未连接服务器。"); return; }
        string cmd=recording?"STOP":"START";
        string ack=QueryBackend(cmd,900);   // STOP 总有 STOPPED 回执；START 无回执
        // 真实状态快照确认（客户端回执 STATE）：停止命令在未录音时也立即
        // 恢复 UI，不伪造成功、不靠超时；STATE 缺失时回退到回执/日志驱动。
        var snap=QueryBackend("STATE",400);
        if(snap!=null) ApplyStateReply(snap);
        else if(cmd=="STOP" && ack=="STOPPED") recording=false;
    }
    static void ReadLog() {
        if(!File.Exists(Log)) return;
        try {
            using(var f=new FileStream(Log,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete)) {
                // 日志被新客户端进程截断重建：偏移复位、历史边界作废（之后全实时）
                if(f.Length<replayBoundary) replayBoundary=0;
                if(f.Length<logPosition){logPosition=0;partialLine="";connected=false;pendingTasks.Clear();replayBoundary=0;}
                if(f.Length==logPosition) return;
                // 历史字节边界：replayBoundary 之前只展示不塑形，之后全实时。
                // 空文件/新日志时 boundary=0，新进程最早的事件不会误判为历史。
                f.Seek(logPosition,SeekOrigin.Begin);   // 增量读取必须先定位
                long segEnd=f.Length;
                bool historical=false;
                if(logPosition<replayBoundary) { historical=true; segEnd=Math.Min(segEnd,replayBoundary); }
                int bytes=(int)(segEnd-logPosition);
                var buf=new byte[bytes];
                int total=0;
                while(total<bytes) {
                    int n=f.Read(buf,total,bytes-total);
                    if(n<=0)break;
                    total+=n;
                }
                logPosition+=total;
                string text=Encoding.UTF8.GetString(buf,0,total);
                var lines=Regex.Split(text,@"\r?\n");
                string tail=lines[lines.Length-1];
                if(historical && logPosition>=replayBoundary) {
                    // 历史段收尾：整行+末尾半行都按历史处理；半行不与实时字节拼接
                    partialLine="";
                    for(int i=0;i<lines.Length-1;i++) ProcessLine(lines[i],true);
                    if(tail.Length>0) ProcessLine(tail,true);
                } else {
                    partialLine+=text;
                    var split=Regex.Split(partialLine,@"\r?\n");
                    partialLine=split[split.Length-1];
                    for(int i=0;i<split.Length-1;i++) ProcessLine(split[i],historical);
                }
            }
        }catch(IOException){}
    }
    // 识别状态机（真实日志驱动，无定时假状态）：
    // - 触发/释放：开始、结束录音（recording）；
    // - 识别等待 = pendingTasks（未完成识别任务集）：客户端“任务生命周期:
    //   提交/完成/发送失败 <task_id>”事件驱动——提交入集、完成/发送失败
    //   按 id 移除；多段识别各自独立，单个 final 只结束自己的任务；
    // - 全部清空（终态兜底）：断线（connected=false 各行）、后端退出
    //   （UpdateDisplay 按 alive 清）、日志截断（ReadLog）。取消/录音任务
    //   错误只复位录音标志，不得清旧在途任务（旧任务的最终结果仍会返回）；
    // - 历史回放（historical）：只进日志显示，不塑形实时状态——GUI 启动
    //   不得因历史 start 无 cancel 而假录音/假识别；接入运行中客户端的
    //   真实状态由 STATE 快照同步。
    static string TaskEventId(string line,string marker) {
        int i=line.IndexOf(marker,StringComparison.Ordinal);
        if(i<0)return null;
        string id=line.Substring(i+marker.Length).Trim();
        return id.Length>0?id:null;
    }
    static void ProcessLine(string line) { ProcessLine(line,false); }
    static void ProcessLine(string line, bool historical) {
        if(line.Contains("WebSocket 建立成功")) { if(!historical) connected=true; }
        if(line.Contains("WebSocket") && (line.Contains("断开")||line.Contains("关闭")||line.Contains("失败"))) { if(!historical) { connected=false; pendingTasks.Clear(); } }
        if(line.Contains("HTTP 401")||line.Contains("令牌")||line.Contains("API Key")) { if(!historical) { connected=false; pendingTasks.Clear(); } }
        string taskId;
        if((taskId=TaskEventId(line,"任务生命周期: 提交 "))!=null) { if(!historical) pendingTasks.Add(taskId); }
        else if((taskId=TaskEventId(line,"任务生命周期: 完成 "))!=null) { if(!historical) pendingTasks.Remove(taskId); }
        else if((taskId=TaskEventId(line,"任务生命周期: 发送失败 "))!=null) { if(!historical) pendingTasks.Remove(taskId); }
        if(line.Contains("触发：开始录音")) { if(!historical) { recording=true; started=DateTime.Now; lastText=""; } }
        if(line.Contains("释放：完成录音")) { if(!historical) recording=false; }
        // 取消/错误只结束录音本身；不清 pendingTasks（旧在途任务不受影响）
        if(line.Contains("录音任务被取消")||line.Contains("取消录音任务")) { if(!historical) recording=false; }
        if(line.Contains("录音任务错误")) { if(!historical) recording=false; }
        // 客户端忽略命令的真实回声：UI 与客户端状态错位时实时校正
        if(line.Contains("忽略 STOP 命令")) { if(!historical) recording=false; }
        if(line.Contains("忽略 START 命令")) { if(!historical) { recording=true; started=DateTime.Now; } }
        int interim=line.IndexOf("实时识别片段:",StringComparison.Ordinal);
        if(interim>=0 && !historical) lastText=line.Substring(interim+"实时识别片段:".Length).Trim();
        int final=line.IndexOf("收到最终识别结果:",StringComparison.Ordinal);
        if(final>=0 && !historical) {
            string piece=line.Substring(final+"收到最终识别结果:".Length).Trim();
            int timing=piece.IndexOf(", 时延:",StringComparison.Ordinal);
            if(timing>=0)piece=piece.Substring(0,timing).Trim();
            piece=Regex.Replace(piece,@"(?:\s*/sil\s*)+$","",RegexOptions.IgnoreCase).Trim();
            lastText=liveMode?lastText+piece:piece;
        }
        if(line.Contains("实时输入已暂停") || line.Contains("最终文字未输入")) AppendLog(line);
        else if(line.Contains("任务生命周期")||line.Contains("服务端未连接")||
            line.Contains("触发：开始录音")||line.Contains("释放：完成录音")||interim>=0||final>=0||
            line.Contains("空语音片段未输出文字")||line.Contains("取消录音任务")||
            line.Contains("录音任务被取消")||line.Contains("录音任务错误")||
            line.Contains("停顿后发送语音")||line.Contains("停顿分段录音完成")||
            line.Contains("忽略 STOP 命令")||line.Contains("忽略 START 命令")||
            line.Contains("WebSocket")||line.Contains("HTTP 401")||line.Contains("令牌")||
            line.Contains("API Key")||line.Contains("ERROR")) AppendLog(line);
    }
    static void AppendLog(string line) {
        logBox.AppendText(line+Environment.NewLine);
        if(logBox.Text.Length>24000) logBox.Text=logBox.Text.Substring(logBox.Text.Length-16000);
        logBox.ScrollToEnd();
    }
    // 识别等待态的录制按钮内容：旋转弧 + “识别中”（相位由 stateAnimTick 推进）
    static object ProcessingContent() {
        mainSpinnerRotate=new RotateTransform(0,8,8);
        mainSpinner=new System.Windows.Shapes.Path {
            Data=Geometry.Parse("M 8,1.5 A 6.5,6.5 0 1 1 1.5,8"),
            Stroke=T("#8A6A32","#E6D5B4"),StrokeThickness=2,
            StrokeStartLineCap=PenLineCap.Round,StrokeEndLineCap=PenLineCap.Round,
            Width=16,Height=16,RenderTransform=mainSpinnerRotate,
            VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(0,0,9,0) };
        var row=new StackPanel { Orientation=Orientation.Horizontal,
            HorizontalAlignment=HorizontalAlignment.Center };
        row.Children.Add(mainSpinner);
        row.Children.Add(Text("识别中",13,"#8A6A32","#E6D5B4",true));
        return row;
    }
    // 识别等待动画的启停：状态本身由日志驱动（processing），这里只切动画相位。
    static void UpdateProcessingAnimation() {
        if(stateAnimTick==null)return;
        if(processing) {
            if(!stateAnimTick.IsEnabled){stateAnimPhase=0;stateAnimTick.Start();}
        } else if(stateAnimTick.IsEnabled) {
            stateAnimTick.Stop();
            if(mainStateDot!=null)mainStateDot.Opacity=1;
            if(sideDot!=null)sideDot.Opacity=1;
            if(floatConnectionDot!=null)floatConnectionDot.Opacity=1;
            if(floatRecord!=null)floatRecord.Opacity=1;
            if(compactRecord!=null)compactRecord.Opacity=1;
        }
    }
    // 呼吸（状态灯/浮窗指示灯）+ 旋转（录制按钮弧）相位推进；仅表达“识别中”。
    static void AnimateProcessingState() {
        stateAnimPhase+=0.17;
        double breath=0.32+0.68*Math.Abs(Math.Sin(stateAnimPhase*1.1));
        if(mainStateDot!=null)mainStateDot.Opacity=breath;
        if(sideDot!=null)sideDot.Opacity=breath;
        if(floatConnectionDot!=null)floatConnectionDot.Opacity=breath;
        if(mainSpinnerRotate!=null)mainSpinnerRotate.Angle=(stateAnimPhase*170)%360;
        if(!recording) {
            if(floatRecord!=null)floatRecord.Opacity=0.62+0.38*breath;
            if(compactRecord!=null)compactRecord.Opacity=0.6+0.4*breath;
        }
    }
    static void UpdateDisplay() {
        bool alive=backend!=null && !backend.HasExited;
        // 后端退出 / 断线：识别等待立即清空（真实终态，不用超时假清除）
        if(!alive || !connected) pendingTasks.Clear();
        if(!alive) recording=false;
        string state=!alive?"客户端未运行":recording?"正在录音  "+(DateTime.Now-started).ToString(@"mm\:ss"):
            processing?"正在识别":connected?"准备就绪":"正在连接服务器";
        status.Text=state;
        heroHint.Text=recording?
            (liveMode?"停顿后发送有效语音，继续说会继续输入":"结束录音后一次回写"):
            processing?(liveMode?"最后一段结果返回中，请稍候":"正在识别整段语音，结果返回后写入"):
            connected?"连接到 "+ServerDisplay()+"  ·  "+ShortcutHint():
            "正在连接 "+ServerDisplay();
        // 状态灯：录音=红、识别等待=琥珀呼吸、已连接=绿、未连接=灰
        string dotHex=recording?RecHex:processing?WarnHex:connected?OkHex:IdleHex;
        mainStateDot.Fill=B(dotHex);
        mainStateDot.ToolTip=recording?"正在录音":processing?"正在识别，请稍候":
            connected?"已连接服务器":"未连接服务器";
        sideStatus.Text=recording?"正在录音":processing?"识别中":connected?"服务已连接":"尚未连接";
        sideDot.Fill=B(dotHex);
        bool floatReady=alive&&connected;
        var floatIndicator=B(recording?RecHex:processing?WarnHex:floatReady?OkHex:WarnHex);
        floatConnectionDot.Fill=floatIndicator;
        floatConnectionDot.ToolTip=recording?"正在录音":processing?"正在识别，请稍候":
            floatReady?"已连接":"未连接服务器";
        floatOuter.BorderBrush=floatReady?T("#55FFFFFF","#44FFFFFF"):B(WarnHex);
        floatStatus.Text=recording?(DateTime.Now-started).ToString(@"mm\:ss"):
            processing?"识别中":"";
        floatDetail.Text=lastText.Length>0?
            (lastText.Length>28?"…"+lastText.Substring(lastText.Length-28):lastText):
            recording?"边说边输入当前应用":processing?"正在识别，请稍候":ServerDisplay();
        transcript.Text=lastText.Length>0?
            (lastText.Length>160?"…"+lastText.Substring(lastText.Length-160):lastText):
            "等待录音。你说的话会出现在这里。";
        // 录制按钮：录音=结束方块；识别等待=旋转指示 + “识别中”；空闲=开始
        // （内容对象在进入该状态时只创建一次，避免每 tick 重建重置旋转角）
        if(processing && !recording) {
            if(!(mainRecord.Content is StackPanel)) mainRecord.Content=ProcessingContent();
        } else {
            string label=recording?"■  结束录音":"●  开始录音";
            if(!(mainRecord.Content is string) || (string)mainRecord.Content!=label) mainRecord.Content=label;
        }
        SetRecordButtonVisual(floatRecord,recording);
        SetRecordButtonVisual(compactRecord,recording);
        ((Ellipse)((Grid)compactRecord.Content).Children[0]).Fill=floatIndicator;
        var floatGlyph=floatRecord.Content as Grid;
        if(floatGlyph!=null && !recording) ((Ellipse)floatGlyph.Children[0]).Fill=floatIndicator;
        string recTip=recording?"点击结束录音":processing?"正在识别，请稍候":
            floatReady?"点击开始录音":"未连接服务器";
        mainRecord.ToolTip=recording?"点击结束录音":processing?"正在识别，请稍候":null;
        compactRecord.ToolTip=recTip;
        floatRecord.ToolTip=recTip;
        UpdateWaveAnimation();
        UpdateProcessingAnimation();
        mainRecord.Background=recording?T("#F3E3E1","#5A3A3C"):processing?T("#F3EADB","#4E4434"):T("#26282B","#EDEEEC");
        mainRecord.Foreground=recording?T("#9E4038","#F4E4E2"):processing?T("#8A6A32","#E6D5B4"):T("#FFFFFF","#1D1F22");
        mainRecord.IsEnabled=floatRecord.IsEnabled=compactRecord.IsEnabled=alive&&connected;
        tray.Text=recording?"CapsWriter · 正在录音":processing?"CapsWriter · 识别中":"CapsWriter · "+(connected?"已连接":"未连接");
    }
    // 返回 false = 无法确认客户端已完成录音停止与输出恢复，
    // 本次退出被放弃（客户端进程保持运行，不 Kill）。
    static bool Exit() {
        if(exiting) return true; exiting=true;
        // 先采样定时器状态再停止：Stop 之后 IsEnabled 恒为 false，若在停止后
        // 采样，握手被取消时定时器永远不会恢复，界面随之失去响应
        bool tickWasRunning=tick!=null && tick.IsEnabled;
        bool waveWasRunning=waveTick!=null && waveTick.IsEnabled;
        bool stateAnimWasRunning=stateAnimTick!=null && stateAnimTick.IsEnabled;
        if(tick!=null) tick.Stop();
        if(waveTick!=null) waveTick.Stop();
        if(stateAnimTick!=null) stateAnimTick.Stop();
        if(!TryPrepareBackendShutdown()) {
            exiting=false;
            if(tickWasRunning) tick.Start();
            if(waveWasRunning) waveTick.Start();
            if(stateAnimWasRunning) stateAnimTick.Start();
            return false;
        }
        if(tray!=null){tray.Visible=false;tray.Dispose();tray=null;}
        if(backend!=null){try{if(ownsBackend&&!backend.HasExited){backend.Kill();backend.WaitForExit(3000);}}catch{}backend.Dispose();backend=null;}
        if(floatWindow!=null) floatWindow.Close();
        if(main!=null) main.Close();
        return true;
    }
}
