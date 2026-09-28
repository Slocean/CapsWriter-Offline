using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;

internal static partial class Desktop {
    sealed class ThemeInk : INotifyPropertyChanged {
        public string Light, Dark;
        public SolidColorBrush Brush;
        public Color CurrentColor {
            get { return (Color)ColorConverter.ConvertFromString(darkTheme ? Dark : Light); }
        }
        public event PropertyChangedEventHandler PropertyChanged;
        public void Refresh() {
            var handler=PropertyChanged;
            if(handler!=null)handler(this,new PropertyChangedEventArgs("CurrentColor"));
        }
    }
    static readonly Dictionary<string, ThemeInk> inks = new Dictionary<string, ThemeInk>();
    static bool darkTheme;
    static bool floatCollapsed;
    static double glassStrength = 55;
    static double floatLeft = Double.NaN, floatTop = Double.NaN;
    static string UiPrefsPath { get { return Path.Combine(Dir, "desktop_ui.ini"); } }
    static Button themeButton, floatCollapseButton, compactRecord;
    static TextBlock compactStatus;
    static TextBlock glassValue;
    static Border floatOuter;
    static UIElement expandedFloat, compactFloat;
    static Slider glassSlider;
    static Button liveModeButton, batchModeButton;
    static TextBlock modeHint;
    static FrameworkElement pauseSetting;
    static bool liveMode=true;
    static bool layoutReady;

    static Brush T(string light, string dark) {
        string key = light + "|" + dark;
        ThemeInk ink;
        if (!inks.TryGetValue(key, out ink)) {
            ink = new ThemeInk { Light = light, Dark = dark,
                Brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(darkTheme ? dark : light)) };
            BindingOperations.SetBinding(ink.Brush,SolidColorBrush.ColorProperty,
                new Binding("CurrentColor") { Source=ink,Mode=BindingMode.OneWay });
            inks.Add(key, ink);
        }
        return ink.Brush;
    }
    static void ApplyTheme() {
        foreach (var ink in inks.Values) ink.Refresh();
        if (themeButton != null) themeButton.Content = darkTheme ? "☀  浅色" : "☾  深色";
        ApplyGlass();
    }
    static TextBlock Text(string value, int size, string light, string dark, bool bold=false) {
        return new TextBlock { Text=value, FontSize=size, Foreground=T(light,dark),
            FontWeight=bold ? FontWeights.SemiBold : FontWeights.Normal,
            VerticalAlignment=VerticalAlignment.Center };
    }
    static Button ThemeButton(string title, string lightBg, string darkBg, string lightFg,
                              string darkFg, double radius=10) {
        var button=Btn(title,lightBg,lightFg,radius);
        button.Background=T(lightBg,darkBg);
        button.Foreground=T(lightFg,darkFg);
        return button;
    }
    static Border Surface(UIElement child, double padding=18) {
        return new Border { Background=T("#FFFFFF","#1B1C1E"),
            BorderBrush=T("#E5E5E3","#383A3D"),BorderThickness=new Thickness(1),
            CornerRadius=new CornerRadius(16),Padding=new Thickness(padding),
            Margin=new Thickness(0,0,0,11),Child=child };
    }
    static UIElement Disclosure(string title, UIElement details) {
        var stack=new StackPanel();
        var head=ThemeButton("", "#FFFFFF","#1B1C1E","#191A1B","#F4F4F2",12);
        head.Height=48;head.HorizontalContentAlignment=HorizontalAlignment.Stretch;
        head.Padding=new Thickness(0);
        var row=new Grid();
        row.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        row.Children.Add(Text(title,13,"#202123","#F0F0EE",true));
        var arrow=Text("⌄",20,"#777A7D","#A6A8A9");
        Grid.SetColumn(arrow,1);row.Children.Add(arrow);
        head.Content=row;
        details.Visibility=Visibility.Collapsed;
        head.Click+=(s,e)=>{
            bool open=details.Visibility!=Visibility.Visible;
            details.Visibility=open?Visibility.Visible:Visibility.Collapsed;
            arrow.Text=open?"⌃":"⌄";
        };
        stack.Children.Add(head);
        stack.Children.Add(details);
        return Surface(stack,17);
    }
    static void UpdateModeButtons() {
        if (liveModeButton==null || batchModeButton==null) return;
        liveModeButton.Background=liveMode?T("#222326","#F0F0EE"):T("#F1F2F0","#303235");
        liveModeButton.Foreground=liveMode?T("#FFFFFF","#1C1D1E"):T("#4A4C4F","#D1D3D3");
        batchModeButton.Background=liveMode?T("#F1F2F0","#303235"):T("#222326","#F0F0EE");
        batchModeButton.Foreground=liveMode?T("#4A4C4F","#D1D3D3"):T("#FFFFFF","#1C1D1E");
        if(modeHint!=null)modeHint.Text=liveMode?
            "说话停顿后发送这一句；持续录音时继续写入。":
            "持续录音，松开按键或结束录音后一次回写。";
        if(pauseSetting!=null)pauseSetting.Visibility=liveMode?Visibility.Visible:Visibility.Collapsed;
    }
    static void SelectMode(bool value) {
        if(liveMode==value)return;
        if(recording || processing) {
            MessageBox.Show("请先结束当前录音和识别，再切换输入方式。");
            return;
        }
        bool previous=liveMode;
        liveMode=value;
        if(!SaveSettings(true))liveMode=previous;
        UpdateModeButtons();
    }
    static Style SlimScrollBar() {
        const string xaml=@"<Style xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'
 xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml'
 TargetType='{x:Type ScrollBar}'>
 <Setter Property='Width' Value='10'/>
 <Setter Property='Background' Value='Transparent'/>
 <Setter Property='Template'><Setter.Value>
  <ControlTemplate TargetType='{x:Type ScrollBar}'>
   <Grid Background='Transparent' Width='10'>
    <Track x:Name='PART_Track' IsDirectionReversed='True'>
     <Track.DecreaseRepeatButton><RepeatButton Command='{x:Static ScrollBar.PageUpCommand}' Opacity='0'/></Track.DecreaseRepeatButton>
     <Track.Thumb><Thumb>
      <Thumb.Template><ControlTemplate TargetType='{x:Type Thumb}'>
       <Border Background='#888B8E' CornerRadius='4' Margin='2,1'/>
      </ControlTemplate></Thumb.Template>
     </Thumb></Track.Thumb>
     <Track.IncreaseRepeatButton><RepeatButton Command='{x:Static ScrollBar.PageDownCommand}' Opacity='0'/></Track.IncreaseRepeatButton>
    </Track>
   </Grid>
  </ControlTemplate>
 </Setter.Value></Setter>
</Style>";
        return (Style)System.Windows.Markup.XamlReader.Parse(xaml);
    }
    static Image AppMark(double size) {
        var image=new Image { Width=size,Height=size,Stretch=Stretch.Uniform };
        var path=Path.Combine(Dir,"assets","icon.png");
        if (File.Exists(path)) {
            var bitmap=new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption=BitmapCacheOption.OnLoad;
            bitmap.UriSource=new Uri(path,UriKind.Absolute);
            bitmap.EndInit(); bitmap.Freeze();
            image.Source=bitmap;
        }
        return image;
    }
    static void LoadUiPrefs() {
        try {
            if (!File.Exists(UiPrefsPath)) return;
            foreach (var line in File.ReadAllLines(UiPrefsPath)) {
                int split=line.IndexOf('=');
                if (split<1) continue;
                string key=line.Substring(0,split), value=line.Substring(split+1);
                double n;
                if (key=="theme") darkTheme=value=="dark";
                else if (key=="glass" && Double.TryParse(value,NumberStyles.Float,CultureInfo.InvariantCulture,out n))
                    glassStrength=Math.Max(0,Math.Min(100,n));
                else if (key=="collapsed") floatCollapsed=value=="1";
                else if (key=="left" && Double.TryParse(value,NumberStyles.Float,CultureInfo.InvariantCulture,out n))
                    floatLeft=n;
                else if (key=="top" && Double.TryParse(value,NumberStyles.Float,CultureInfo.InvariantCulture,out n))
                    floatTop=n;
            }
        } catch (IOException) {}
    }
    static void SaveUiPrefs() {
        try {
            string data="theme="+(darkTheme?"dark":"light")+"\n"+
                "glass="+glassStrength.ToString("F0",CultureInfo.InvariantCulture)+"\n"+
                "collapsed="+(floatCollapsed?"1":"0")+"\n"+
                "left="+floatLeft.ToString("F0",CultureInfo.InvariantCulture)+"\n"+
                "top="+floatTop.ToString("F0",CultureInfo.InvariantCulture)+"\n";
            File.WriteAllText(UiPrefsPath,data);
        } catch (IOException) {} catch (UnauthorizedAccessException) {}
    }
    static void SetFloatCollapsed(bool value) {
        floatCollapsed=value;
        if (floatWindow==null || expandedFloat==null || compactFloat==null) return;
        expandedFloat.Visibility=value?Visibility.Collapsed:Visibility.Visible;
        compactFloat.Visibility=value?Visibility.Visible:Visibility.Collapsed;
        floatWindow.Width=value?258:374;
        floatWindow.Height=value?70:112;
        if (floatCollapseButton!=null) floatCollapseButton.Content="−";
        var work=SystemParameters.WorkArea;
        floatWindow.Left=Math.Max(work.Left,Math.Min(floatWindow.Left,work.Right-floatWindow.Width));
        floatWindow.Top=Math.Max(work.Top,Math.Min(floatWindow.Top,work.Bottom-floatWindow.Height));
        ApplyGlass();
        SaveUiPrefs();
    }
    static void ClipFloatCorners() {
        if(floatWindow==null)return;
        var hwnd=new System.Windows.Interop.WindowInteropHelper(floatWindow).Handle;
        if(hwnd==IntPtr.Zero)return;
        var source=PresentationSource.FromVisual(floatWindow);
        double sx=source!=null?source.CompositionTarget.TransformToDevice.M11:1;
        double sy=source!=null?source.CompositionTarget.TransformToDevice.M22:1;
        int width=(int)Math.Ceiling(floatWindow.Width*sx);
        int height=(int)Math.Ceiling(floatWindow.Height*sy);
        IntPtr region=CreateRoundRectRgn(0,0,width+1,height+1,
            (int)Math.Round(52*sx),(int)Math.Round(52*sy));
        if(region!=IntPtr.Zero && SetWindowRgn(hwnd,region,true)==0)DeleteObject(region);
    }
    static void ApplyGlass() {
        if (floatOuter==null) return;
        byte alpha=(byte)(75+glassStrength*1.5);
        var baseColor=darkTheme?Color.FromArgb(alpha,23,24,26):Color.FromArgb(alpha,248,248,246);
        floatOuter.Background=new SolidColorBrush(baseColor);
        if (glassValue!=null) glassValue.Text=((int)Math.Round(glassStrength)).ToString()+"%";
        if (floatWindow==null || new System.Windows.Interop.WindowInteropHelper(floatWindow).Handle==IntPtr.Zero) return;
        var h=new System.Windows.Interop.WindowInteropHelper(floatWindow).Handle;
        ClipFloatCorners();
        AccentPolicy accent=new AccentPolicy();
        accent.AccentState=glassStrength<1?2:4; // transparent or Windows acrylic blur
        accent.AccentFlags=2;
        int tint=darkTheme?0x18:0xF8;
        accent.GradientColor=(alpha<<24)|(tint<<16)|(tint<<8)|tint;
        int size=Marshal.SizeOf(typeof(AccentPolicy));
        IntPtr memory=Marshal.AllocHGlobal(size);
        try {
            Marshal.StructureToPtr(accent,memory,false);
            var attribute=new WindowCompositionAttributeData { Attribute=19,Data=memory,SizeOfData=size };
            try { SetWindowCompositionAttribute(h,ref attribute); }
            catch (EntryPointNotFoundException) {}
        } finally { Marshal.FreeHGlobal(memory); }
    }
    [StructLayout(LayoutKind.Sequential)]
    struct AccentPolicy { public int AccentState, AccentFlags, GradientColor, AnimationId; }
    [StructLayout(LayoutKind.Sequential)]
    struct WindowCompositionAttributeData { public int Attribute; public IntPtr Data; public int SizeOfData; }
    [DllImport("user32.dll")]
    static extern int SetWindowCompositionAttribute(IntPtr hwnd, ref WindowCompositionAttributeData data);
    [DllImport("gdi32.dll")]
    static extern IntPtr CreateRoundRectRgn(int left,int top,int right,int bottom,int ellipseWidth,int ellipseHeight);
    [DllImport("user32.dll")]
    static extern int SetWindowRgn(IntPtr hwnd,IntPtr region,bool redraw);
    [DllImport("gdi32.dll")]
    static extern bool DeleteObject(IntPtr obj);
}
