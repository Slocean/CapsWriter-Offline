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
    static TextBox host, port, seconds, contextWords, logBox;
    static TextBlock status, transcript, floatStatus, floatDetail, heroHint, sideStatus;
    static Ellipse sideDot, floatDot;
    static Rectangle[] heroBars;
    static int waveFrame;
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
    static Brush HeroBrush() {
        return new LinearGradientBrush(
            (Color)ColorConverter.ConvertFromString("#17355F"),
            (Color)ColorConverter.ConvertFromString("#23588A"), 25);
    }
    static TextBlock Label(string value, int size=13, string color="#748199", bool strong=false) {
        return new TextBlock { Text=value, FontSize=size, Foreground=B(color),
            FontWeight=strong?FontWeights.SemiBold:FontWeights.Normal,
            VerticalAlignment=VerticalAlignment.Center };
    }
    static ControlTemplate ButtonTemplate(double radius) {
        var border=new FrameworkElementFactory(typeof(Border));
        border.SetValue(Border.CornerRadiusProperty,new CornerRadius(radius));
        border.SetBinding(Border.BackgroundProperty,new System.Windows.Data.Binding("Background") {
            RelativeSource=new System.Windows.Data.RelativeSource(System.Windows.Data.RelativeSourceMode.TemplatedParent) });
        var content=new FrameworkElementFactory(typeof(ContentPresenter));
        content.SetValue(FrameworkElement.HorizontalAlignmentProperty,HorizontalAlignment.Center);
        content.SetValue(FrameworkElement.VerticalAlignmentProperty,VerticalAlignment.Center);
        border.AppendChild(content);
        return new ControlTemplate(typeof(Button)) { VisualTree=border };
    }
    static Button Btn(string value,string bg="#346BEE",string fg="#FFFFFF",double radius=10) {
        return new Button { Content=value, Height=40, Padding=new Thickness(16,0,16,0),
            Background=B(bg), Foreground=B(fg), BorderThickness=new Thickness(0),
            FontSize=13, FontWeight=FontWeights.SemiBold, Cursor=Cursors.Hand,
            Template=ButtonTemplate(radius) };
    }
    static TextBox Field() {
        return new TextBox { Height=42, FontSize=14, Foreground=B("#20314E"),
            Background=B("#F8FAFD"), BorderBrush=B("#DBE3EE"), BorderThickness=new Thickness(1),
            Padding=new Thickness(11,9,11,7), VerticalContentAlignment=VerticalAlignment.Center };
    }
    static Border Card(UIElement child,double padding=20) {
        return new Border { Background=Brushes.White, BorderBrush=B("#E4EAF3"),
            BorderThickness=new Thickness(1), CornerRadius=new CornerRadius(17),
            Padding=new Thickness(padding), Margin=new Thickness(0,0,0,16), Child=child };
    }
    static CheckBox Switch(string title) {
        var toggle=new CheckBox { Content=title, Foreground=B("#334663"), FontSize=13,
            VerticalAlignment=VerticalAlignment.Center, Cursor=Cursors.Hand };
        var root=new FrameworkElementFactory(typeof(StackPanel));
        root.SetValue(StackPanel.OrientationProperty,Orientation.Horizontal);
        var track=new FrameworkElementFactory(typeof(Border));
        track.Name="Track";
        track.SetValue(Border.WidthProperty,36.0);
        track.SetValue(Border.HeightProperty,21.0);
        track.SetValue(Border.CornerRadiusProperty,new CornerRadius(11));
        track.SetValue(Border.BackgroundProperty,B("#C4CEDC"));
        track.SetValue(Border.MarginProperty,new Thickness(0,0,10,0));
        var thumb=new FrameworkElementFactory(typeof(Ellipse));
        thumb.Name="Thumb";
        thumb.SetValue(FrameworkElement.WidthProperty,15.0);
        thumb.SetValue(FrameworkElement.HeightProperty,15.0);
        thumb.SetValue(FrameworkElement.MarginProperty,new Thickness(3,0,3,0));
        thumb.SetValue(FrameworkElement.HorizontalAlignmentProperty,HorizontalAlignment.Left);
        thumb.SetValue(Shape.FillProperty,Brushes.White);
        track.AppendChild(thumb);
        root.AppendChild(track);
        var caption=new FrameworkElementFactory(typeof(ContentPresenter));
        caption.SetValue(FrameworkElement.VerticalAlignmentProperty,VerticalAlignment.Center);
        root.AppendChild(caption);
        var template=new ControlTemplate(typeof(CheckBox)) { VisualTree=root };
        var on=new Trigger { Property=System.Windows.Controls.Primitives.ToggleButton.IsCheckedProperty, Value=true };
        on.Setters.Add(new Setter(Border.BackgroundProperty,B("#3773EA"),"Track"));
        on.Setters.Add(new Setter(FrameworkElement.HorizontalAlignmentProperty,HorizontalAlignment.Right,"Thumb"));
        template.Triggers.Add(on);
        toggle.Template=template;
        return toggle;
    }
    static StackPanel Wave(int count,string color,out Rectangle[] bars) {
        var panel=new StackPanel { Orientation=Orientation.Horizontal,HorizontalAlignment=HorizontalAlignment.Center,
            VerticalAlignment=VerticalAlignment.Center };
        bars=new Rectangle[count];
        for(int i=0;i<count;i++) {
            var bar=new Rectangle { Width=6,Height=12,RadiusX=3,RadiusY=3,
                Fill=B(color),Margin=new Thickness(3,0,3,0),VerticalAlignment=VerticalAlignment.Center };
            bars[i]=bar; panel.Children.Add(bar);
        }
        return panel;
    }
    static void BuildMain() {
        main=new Window { Title="CapsWriter · 语音输入", Width=810, Height=700,
            MinWidth=690, MinHeight=590, WindowStartupLocation=WindowStartupLocation.CenterScreen,
            WindowStyle=WindowStyle.None, ResizeMode=ResizeMode.CanResize,
            Background=B("#F4F7FB"), FontFamily=new FontFamily("Microsoft YaHei UI") };
        System.Windows.Shell.WindowChrome.SetWindowChrome(main,new System.Windows.Shell.WindowChrome {
            CaptionHeight=0, ResizeBorderThickness=new Thickness(6),
            GlassFrameThickness=new Thickness(0), CornerRadius=new CornerRadius(0),
            UseAeroCaptionButtons=false });
        main.Closing+=(s,e)=>{if(!exiting){e.Cancel=true;main.Hide();}};
        var root=new Grid { Background=B("#F4F7FB") };
        root.RowDefinitions.Add(new RowDefinition { Height=new GridLength(64) });
        root.RowDefinitions.Add(new RowDefinition { Height=new GridLength(1,GridUnitType.Star) });

        var top=new Border { Background=Brushes.White, BorderBrush=B("#E2E8F1"),
            BorderThickness=new Thickness(0,0,0,1) };
        var topGrid=new Grid { Margin=new Thickness(22,0,15,0) };
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        var brand=new StackPanel { Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center };
        var logo=new Border { Width=34,Height=34,CornerRadius=new CornerRadius(10),
            Background=B("#46D7BC"),Margin=new Thickness(0,0,11,0) };
        logo.Child=new TextBlock { Text="C",FontSize=18,FontWeight=FontWeights.Bold,
            Foreground=B("#10213A"),HorizontalAlignment=HorizontalAlignment.Center,
            VerticalAlignment=VerticalAlignment.Center };
        brand.Children.Add(logo);
        var brandName=new StackPanel { VerticalAlignment=VerticalAlignment.Center };
        brandName.Children.Add(Label("CapsWriter",15,"#1C2E4A",true));
        brandName.Children.Add(Label("语音输入",10,"#8997AB"));
        brand.Children.Add(brandName);
        brand.MouseLeftButtonDown+=(s,e)=>{try{main.DragMove();}catch{}};
        topGrid.Children.Add(brand);

        var connection=new Border { Background=B("#EDF8F4"),CornerRadius=new CornerRadius(12),
            Padding=new Thickness(10,6,11,6),VerticalAlignment=VerticalAlignment.Center,
            Margin=new Thickness(0,0,18,0) };
        var connectionRow=new StackPanel { Orientation=Orientation.Horizontal };
        sideDot=new Ellipse { Width=8,Height=8,Fill=B("#E9B65D"),
            Margin=new Thickness(0,0,7,0),VerticalAlignment=VerticalAlignment.Center };
        connectionRow.Children.Add(sideDot);
        sideStatus=Label("连接中",11,"#2E826C",true);connectionRow.Children.Add(sideStatus);
        connection.Child=connectionRow;Grid.SetColumn(connection,1);topGrid.Children.Add(connection);

        var chromeButtons=new StackPanel { Orientation=Orientation.Horizontal,
            VerticalAlignment=VerticalAlignment.Center };
        var minimize=Btn("−","#FFFFFF","#667892",8);
        minimize.Width=36;minimize.Height=34;minimize.FontSize=19;
        minimize.Click+=(s,e)=>main.WindowState=WindowState.Minimized;
        chromeButtons.Children.Add(minimize);
        var close=Btn("×","#FFFFFF","#667892",8);
        close.Width=36;close.Height=34;close.FontSize=18;
        close.Click+=(s,e)=>main.Hide();
        chromeButtons.Children.Add(close);
        Grid.SetColumn(chromeButtons,2);topGrid.Children.Add(chromeButtons);
        top.Child=topGrid;Grid.SetRow(top,0);root.Children.Add(top);

        var scroll=new ScrollViewer { VerticalScrollBarVisibility=ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled };
        var body=new StackPanel { Margin=new Thickness(22,20,22,18) };

        var hero=new Border { Height=164,Background=HeroBrush(),CornerRadius=new CornerRadius(19),
            Margin=new Thickness(0,0,0,12) };
        var heroGrid=new Grid { Margin=new Thickness(23,18,23,17) };
        heroGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        heroGrid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(136) });
        var heroLeft=new StackPanel();
        heroLeft.Children.Add(Label("语音输入  /  LIVE DICTATION",10,"#A7D5EE",true));
        status=Label("准备开始",24,"#FFFFFF",true);status.Margin=new Thickness(0,7,0,0);
        heroLeft.Children.Add(status);
        heroHint=Label("正在连接服务器…",11,"#C3DCEF");
        heroHint.Margin=new Thickness(0,3,0,12);heroLeft.Children.Add(heroHint);
        mainRecord=Btn("●  开始录音","#FFFFFF","#1D4D82",10);
        mainRecord.Width=144;mainRecord.Height=40;mainRecord.HorizontalAlignment=HorizontalAlignment.Left;
        mainRecord.Click+=(s,e)=>{bool start=!recording;if(connected){ToggleRecording();if(start)main.WindowState=WindowState.Minimized;}};
        heroLeft.Children.Add(mainRecord);heroGrid.Children.Add(heroLeft);
        var waveHost=new Border { Background=B("#2A5D8F"),CornerRadius=new CornerRadius(15),
            Margin=new Thickness(9,10,0,10) };
        waveHost.Child=Wave(8,"#AEDCF1",out heroBars);
        Grid.SetColumn(waveHost,1);heroGrid.Children.Add(waveHost);
        hero.Child=heroGrid;body.Children.Add(hero);

        var resultPanel=new StackPanel();
        var resultHead=new Grid();
        resultHead.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        resultHead.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        resultHead.Children.Add(Label("实时文字",14,"#243651",true));
        var liveTag=new Border { Background=B("#EDF8F4"),CornerRadius=new CornerRadius(10),
            Padding=new Thickness(8,3,8,3) };
        liveTag.Child=Label("●  输入当前应用",10,"#2A9A7B",true);
        Grid.SetColumn(liveTag,1);resultHead.Children.Add(liveTag);
        resultPanel.Children.Add(resultHead);
        transcript=Label("等待录音。你说的话会出现在这里。",14,"#77879D");
        transcript.TextWrapping=TextWrapping.Wrap;
        transcript.Margin=new Thickness(0,10,0,2);
        transcript.MaxHeight=58;
        resultPanel.Children.Add(transcript);
        body.Children.Add(Card(resultPanel,17));

        var serverPanel=new StackPanel();
        var serverHeading=new Grid();
        serverHeading.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        serverHeading.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        serverHeading.Children.Add(Label("服务器连接",14,"#243651",true));
        var restart=Btn("重启客户端","#EEF3F9","#49617D",8);
        restart.Width=100;restart.Height=30;restart.FontSize=11;
        restart.Click+=(s,e)=>RestartBackend();
        Grid.SetColumn(restart,1);serverHeading.Children.Add(restart);
        serverPanel.Children.Add(serverHeading);
        var fields=new Grid { Margin=new Thickness(0,12,0,0) };
        fields.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(2,GridUnitType.Star) });
        fields.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        fields.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        var addrCol=new StackPanel { Margin=new Thickness(0,0,12,0) };
        addrCol.Children.Add(Label("地址",11,"#718098",true));
        host=Field();host.Margin=new Thickness(0,6,0,0);addrCol.Children.Add(host);
        fields.Children.Add(addrCol);
        var portCol=new StackPanel { Margin=new Thickness(0,0,12,0) };
        portCol.Children.Add(Label("端口",11,"#718098",true));
        port=Field();port.Margin=new Thickness(0,6,0,0);portCol.Children.Add(port);
        Grid.SetColumn(portCol,1);fields.Children.Add(portCol);
        var save=Btn("保存并重连");save.Width=116;save.Height=42;
        save.VerticalAlignment=VerticalAlignment.Bottom;
        save.Click+=(s,e)=>SaveSettings(true);
        Grid.SetColumn(save,2);fields.Children.Add(save);
        serverPanel.Children.Add(fields);
        showFloat=Switch("显示浮窗");showFloat.Margin=new Thickness(0,15,0,0);
        showFloat.Checked+=(s,e)=>{if(floatWindow!=null)floatWindow.Show();};
        showFloat.Unchecked+=(s,e)=>{if(floatWindow!=null)floatWindow.Hide();};
        serverPanel.Children.Add(showFloat);
        body.Children.Add(Card(serverPanel,17));

        var advancedPanel=new StackPanel();
        var secondsRow=new StackPanel { Orientation=Orientation.Horizontal,Margin=new Thickness(0,12,0,0) };
        var secondsCol=new StackPanel { Width=140,Margin=new Thickness(0,0,14,0) };
        secondsCol.Children.Add(Label("分段秒数",11,"#718098",true));
        seconds=Field();seconds.Margin=new Thickness(0,6,0,0);secondsCol.Children.Add(seconds);
        secondsRow.Children.Add(secondsCol);
        var shortcutCol=new StackPanel { VerticalAlignment=VerticalAlignment.Bottom,
            Margin=new Thickness(0,0,0,11) };
        capsHotkey=Switch("CapsLock 长按");shortcutCol.Children.Add(capsHotkey);
        secondsRow.Children.Add(shortcutCol);
        advancedPanel.Children.Add(secondsRow);
        var contextCol=new StackPanel { Margin=new Thickness(0,13,0,0) };
        contextCol.Children.Add(Label("识别提示词 · 可选",11,"#718098",true));
        contextWords=Field();contextWords.Margin=new Thickness(0,6,0,0);
        contextCol.Children.Add(contextWords);
        var contextHint=Label("如：浮窗、托盘。提示模型理解常用词，不能保证纠错。",11,"#91A0B3");
        contextHint.Margin=new Thickness(0,5,0,0);contextCol.Children.Add(contextHint);
        advancedPanel.Children.Add(contextCol);
        var advanced=new Expander { Header=Label("识别与快捷键设置",13,"#40536D",true),
            Content=advancedPanel,IsExpanded=false };
        body.Children.Add(Card(advanced,14));

        var logBoxPanel=new StackPanel();
        logBox=new TextBox { Height=105,Margin=new Thickness(0,10,0,0),IsReadOnly=true,
            TextWrapping=TextWrapping.NoWrap,VerticalScrollBarVisibility=ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility=ScrollBarVisibility.Auto,Background=B("#F6F8FC"),
            Foreground=B("#60718B"),BorderThickness=new Thickness(0),Padding=new Thickness(10),
            FontFamily=new FontFamily("Consolas"),FontSize=11 };
        logBoxPanel.Children.Add(logBox);
        var logExpander=new Expander { Header=Label("运行记录",13,"#40536D",true),
            Content=logBoxPanel,IsExpanded=false };
        body.Children.Add(Card(logExpander,14));
        scroll.Content=body;Grid.SetRow(scroll,1);root.Children.Add(scroll);
        main.Content=root;
    }

    static void BuildFloat() {
        floatWindow=new Window { Width=386,Height=100,WindowStyle=WindowStyle.None,
            ResizeMode=ResizeMode.NoResize,AllowsTransparency=true,Background=Brushes.Transparent,
            Topmost=true,ShowInTaskbar=false,ShowActivated=false,
            FontFamily=new FontFamily("Microsoft YaHei UI") };
        var work=SystemParameters.WorkArea;
        floatWindow.Left=work.Right-floatWindow.Width-22;
        floatWindow.Top=work.Bottom-floatWindow.Height-22;
        floatWindow.SourceInitialized+=(s,e)=>{
            var h=new WindowInteropHelper(floatWindow).Handle;
            SetWindowLong(h,-20,GetWindowLong(h,-20)|0x08000000|0x80);
        };
        floatWindow.Closing+=(s,e)=>{if(!exiting){e.Cancel=true;floatWindow.Hide();showFloat.IsChecked=false;}};
        var outer=new Border { Background=B("#13253F"),BorderBrush=B("#35506D"),
            BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(21),
            Padding=new Thickness(15,11,12,11),
            Effect=new DropShadowEffect { Color=Colors.Black,Opacity=.35,BlurRadius=18,ShadowDepth=4 } };
        var grid=new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(14) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(58) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(24) });
        floatDot=new Ellipse { Width=9,Height=9,Fill=B("#4AD5AE"),VerticalAlignment=VerticalAlignment.Center };
        Grid.SetColumn(floatDot,0);grid.Children.Add(floatDot);
        var labels=new StackPanel { VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(5,0,8,0) };
        labels.Children.Add(Label("CAPSWRITER  ·  VOICE",9,"#78AAC7",true));
        floatStatus=Label("连接中",15,"#F3F9FF",true);floatStatus.Margin=new Thickness(0,4,0,0);
        labels.Children.Add(floatStatus);
        floatDetail=Label("等待客户端连接",11,"#A4BCD2");
        floatDetail.Margin=new Thickness(0,3,0,0);floatDetail.TextTrimming=TextTrimming.CharacterEllipsis;
        labels.Children.Add(floatDetail);
        labels.MouseLeftButtonDown+=(s,e)=>{try{floatWindow.DragMove();}catch{}};
        Grid.SetColumn(labels,1);grid.Children.Add(labels);
        floatRecord=Btn("●","#3B77F2","#FFFFFF",28);
        floatRecord.Width=54;floatRecord.Height=54;floatRecord.FontSize=23;
        floatRecord.ToolTip="点击开始或结束录音";
        floatRecord.Click+=(s,e)=>ToggleRecording();
        Grid.SetColumn(floatRecord,2);grid.Children.Add(floatRecord);
        var close=Btn("×","#13253F","#859FB9",10);
        close.Width=22;close.Height=27;close.FontSize=17;
        close.VerticalAlignment=VerticalAlignment.Top;close.Margin=new Thickness(1,-7,0,0);
        close.ToolTip="隐藏浮窗";
        close.Click+=(s,e)=>{floatWindow.Hide();showFloat.IsChecked=false;};
        Grid.SetColumn(close,3);grid.Children.Add(close);
        outer.Child=grid;floatWindow.Content=outer;
    }

    sealed class TrayColors : Forms.ProfessionalColorTable {
        readonly System.Drawing.Color bg=System.Drawing.Color.FromArgb(19,37,63);
        readonly System.Drawing.Color hover=System.Drawing.Color.FromArgb(42,73,108);
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
            BackColor=System.Drawing.Color.FromArgb(19,37,63),
            ForeColor=System.Drawing.Color.FromArgb(239,246,255),
            Font=new System.Drawing.Font("Microsoft YaHei UI",9.5f),
            Padding=new Forms.Padding(7,6,7,6) };
        menu.Renderer=new Forms.ToolStripProfessionalRenderer(new TrayColors());
        menu.Items.Add("打开主界面",null,(s,e)=>main.Dispatcher.Invoke(()=>{main.Show();main.Activate();}));
        menu.Items.Add("显示 / 隐藏浮窗",null,(s,e)=>main.Dispatcher.Invoke(()=>showFloat.IsChecked=showFloat.IsChecked!=true));
        menu.Items.Add("开始 / 停止录音",null,(s,e)=>main.Dispatcher.Invoke(()=>ToggleRecording()));
        menu.Items.Add("退出",null,(s,e)=>main.Dispatcher.Invoke(()=>{Exit();Application.Current.Shutdown();}));
        foreach(Forms.ToolStripItem item in menu.Items) {
            item.ForeColor=System.Drawing.Color.FromArgb(239,246,255);
            item.Padding=new Forms.Padding(6,5,6,5);
        }
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
        contextWords.Text=Value(content,"context","");
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
        string prompt=contextWords.Text.Trim();
        if(prompt.Length>120 || prompt.IndexOfAny(new[]{'\r','\n'})>=0) {
            MessageBox.Show("识别提示词最多 120 字，不能包含换行。"); return false;
        }
        var content=ReadConfig();
        string updated=content;
        updated=Set(updated,"addr","'"+hostname+"'");
        updated=Set(updated,"port","'"+pn+"'");
        updated=Set(updated,"mic_seg_duration",duration.ToString(System.Globalization.CultureInfo.InvariantCulture));
        updated=Set(updated,"mic_seg_overlap","0.5");
        updated=Set(updated,"context","'"+prompt.Replace("\\","\\\\").Replace("'","\\'")+"'");
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
        string state=!alive?"客户端未运行":recording?"正在录音  "+(DateTime.Now-started).ToString(@"mm\:ss"):
            processing?"正在识别":connected?"准备就绪":"正在连接服务器";
        status.Text=state;
        heroHint.Text=recording?"正在分段识别，文字会进入当前应用":
            connected?"连接到 "+host.Text+":"+port.Text+"  ·  按住 CapsLock 或点击浮窗录音":
            "正在连接 "+host.Text+":"+port.Text;
        sideStatus.Text=recording?"正在录音":connected?"服务已连接":"尚未连接";
        sideDot.Fill=B(recording?"#F77D82":connected?"#49D6AF":"#E9B65D");
        floatDot.Fill=B(recording?"#F77D82":connected?"#49D6AF":"#E9B65D");
        floatStatus.Text=recording?"正在录音  "+(DateTime.Now-started).ToString(@"mm\:ss"):
            processing?"正在识别":connected?"已连接 · 待机":"正在连接";
        floatDetail.Text=lastText.Length>0?
            (lastText.Length>28?"…"+lastText.Substring(lastText.Length-28):lastText):
            recording?"边说边输入当前应用":host.Text+":"+port.Text;
        transcript.Text=lastText.Length>0?
            (lastText.Length>160?"…"+lastText.Substring(lastText.Length-160):lastText):
            "等待录音。你说的话会出现在这里。";
        mainRecord.Content=recording?"■  结束录音":"●  开始录音";
        floatRecord.Content=recording?"■":"●";
        mainRecord.Background=B(recording?"#FFE9EA":"#FFFFFF");
        mainRecord.Foreground=B(recording?"#BE4351":"#1D4D82");
        floatRecord.Background=B(recording?"#E85C68":"#3B77F2");
        mainRecord.IsEnabled=floatRecord.IsEnabled=alive&&connected;
        waveFrame++;
        if(heroBars!=null)for(int i=0;i<heroBars.Length;i++)
            heroBars[i].Height=recording?18+((i*11+waveFrame*7)%37):9+((i*5)%10);
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
