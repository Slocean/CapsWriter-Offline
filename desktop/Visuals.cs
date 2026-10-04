using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Rectangle = System.Windows.Shapes.Rectangle;
using Ellipse = System.Windows.Shapes.Ellipse;

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
    static TextBlock glassValue;
    static Border floatOuter;
    static System.Windows.Threading.DispatcherTimer waveTick;
    static readonly List<Rectangle> waveBars = new List<Rectangle>();
    static double wavePhase;
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

    // ===== 统一低饱和色板（与网页端 admin/static/app.css 同一色系）=====
    // 文字层级：标题 > 小节标题 > 次要 > 辅助说明
    static TextBlock SectionTitle(string value) { return Text(value,13,"#33363A","#E7E8E6",true); }
    static TextBlock Hint(string value) { return Text(value,11,"#7A7E83","#A2A5A9"); }
    static TextBlock FieldLabel(string value) { return Text(value,11,"#7A7E83","#A2A5A9"); }
    // 表面与控件底色
    static Brush PageBg() { return T("#F6F7F5","#141517"); }
    static Brush CardBg() { return T("#FFFFFF","#1D1F22"); }
    static Brush CardLine() { return T("#E6E7E4","#35383C"); }
    static Brush ChipBg() { return T("#F0F1EE","#2C2F32"); }
    static Brush FieldBg() { return T("#FBFBFA","#232529"); }
    static Brush FieldLine() { return T("#D8DAD6","#484B50"); }
    static Brush FieldInk() { return T("#202226","#F0F1EF"); }
    // 语义状态色（连接/录音/告警），低饱和
    static string OkHex { get { return darkTheme?"#6FBC92":"#5FA982"; } }
    static string RecHex { get { return darkTheme?"#E5837C":"#D96A64"; } }
    static string WarnHex { get { return darkTheme?"#D4A459":"#D99A4E"; } }
    static string IdleHex { get { return darkTheme?"#8D9094":"#A9ACAF"; } }

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
        return new Border { Background=CardBg(),
            BorderBrush=CardLine(),BorderThickness=new Thickness(1),
            CornerRadius=new CornerRadius(14),Padding=new Thickness(padding),
            Margin=new Thickness(0,0,0,12),Child=child };
    }
    static UIElement Disclosure(string title, UIElement details) {
        var stack=new StackPanel();
        var head=ThemeButton("", "#FFFFFF","#1D1F22","#33363A","#E7E8E6",12);
        head.Height=48;head.HorizontalContentAlignment=HorizontalAlignment.Stretch;
        head.Padding=new Thickness(0);
        var row=new Grid();
        row.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        row.Children.Add(Text(title,13,"#33363A","#E7E8E6",true));
        var arrow=Text("⌄",20,"#7A7E83","#A2A5A9");
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
        // 分段选中=主按钮色对；未选中=次级色对（与 SaveSettings 的主/次按钮同一套）
        liveModeButton.Background=liveMode?T("#26282B","#EDEEEC"):T("#F0F1EE","#2E3134");
        liveModeButton.Foreground=liveMode?T("#FFFFFF","#1D1F22"):T("#5A5D62","#C7C9C8");
        batchModeButton.Background=liveMode?T("#F0F1EE","#2E3134"):T("#26282B","#EDEEEC");
        batchModeButton.Foreground=liveMode?T("#5A5D62","#C7C9C8"):T("#FFFFFF","#1D1F22");
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
       <Border Background='#9A9DA1' CornerRadius='4' Margin='2,1'/>
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
        floatWindow.Width=value?116:320;
        floatWindow.Height=value?56:108;
        if (floatCollapseButton!=null) floatCollapseButton.Content="−";
        var work=SystemParameters.WorkArea;
        floatWindow.Left=Math.Max(work.Left,Math.Min(floatWindow.Left,work.Right-floatWindow.Width));
        floatWindow.Top=Math.Max(work.Top,Math.Min(floatWindow.Top,work.Bottom-floatWindow.Height));
        ApplyGlass();
        SaveUiPrefs();
    }
    static Ellipse ConnectionDot() {
        return new Ellipse { Width=8,Height=8,Fill=B(WarnHex),
            VerticalAlignment=VerticalAlignment.Center,ToolTip="正在连接服务器" };
    }
    static Button WindowAction(string kind) {
        var button=ThemeButton("","#00000000","#00000000","#63666A","#D5D7D5",8);
        button.Width=21;button.Height=20;button.Padding=new Thickness(0);
        button.Opacity=.76;
        var path=new System.Windows.Shapes.Path {
            Data=Geometry.Parse(kind=="close"?
                "M 3,3 L 11,11 M 11,3 L 3,11":
                kind=="expand"?"M 3,9 L 7,5 L 11,9":"M 3,5 L 7,9 L 11,5"),
            Stroke=T("#63666A","#D5D7D5"),StrokeThickness=1.6,
            StrokeStartLineCap=PenLineCap.Round,StrokeEndLineCap=PenLineCap.Round,
            Width=14,Height=14,Stretch=Stretch.None };
        button.Content=path;
        button.MouseEnter+=(s,e)=>button.Opacity=1;
        button.MouseLeave+=(s,e)=>button.Opacity=.76;
        return button;
    }
    static Border WindowActions(Button first, Button second) {
        var row=new StackPanel { Orientation=Orientation.Horizontal };
        row.Children.Add(first);row.Children.Add(second);
        return new Border { Background=T("#11000000","#22FFFFFF"),
            BorderBrush=T("#18000000","#26FFFFFF"),
            BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(12),
            Child=row,VerticalAlignment=VerticalAlignment.Center };
    }
    static Button RecordButton(double size) {
        var button=ThemeButton("", "#26282B","#EDEEEC","#FFFFFF","#1D1F22",size/2);
        button.Width=size;button.Height=size;button.Padding=new Thickness(0);
        button.ToolTip="点击开始录音";
        var glyph=new Grid { Width=28,Height=28 };
        glyph.Children.Add(new Ellipse { Width=size>=50?18:15,Height=size>=50?18:15,
            Fill=T("#FFFFFF","#1D1F22"),HorizontalAlignment=HorizontalAlignment.Center,
            VerticalAlignment=VerticalAlignment.Center });
        glyph.Children.Add(new Border { Width=size>=50?25:21,Height=size>=50?25:21,
            CornerRadius=new CornerRadius(5),Background=T("#26282B","#F1F2F0"),
            HorizontalAlignment=HorizontalAlignment.Center,
            VerticalAlignment=VerticalAlignment.Center,
            Visibility=Visibility.Collapsed });
        button.Content=glyph;
        return button;
    }
    static void SetRecordButtonVisual(Button button, bool active) {
        if(button==null)return;
        var glyph=button.Content as Grid;
        if(glyph==null)return;
        glyph.Children[0].Visibility=active?Visibility.Collapsed:Visibility.Visible;
        glyph.Children[1].Visibility=active?Visibility.Visible:Visibility.Collapsed;
        // 录音态=浅红底 + 深色方块；空闲=主按钮色对
        button.Background=active?T("#EBDCD9","#4E3A3B"):T("#26282B","#EDEEEC");
        button.ToolTip=active?"点击结束录音":"点击开始录音";
    }
    static StackPanel WaveBars(int count) {
        var row=new StackPanel { Orientation=Orientation.Horizontal,
            VerticalAlignment=VerticalAlignment.Center,Opacity=0 };
        for(int i=0;i<count;i++) {
            var bar=new Rectangle { Width=3,Height=4,RadiusX=1.5,RadiusY=1.5,
                Fill=T("#63666A","#E1E2DF"),Margin=new Thickness(2,0,2,0),
                VerticalAlignment=VerticalAlignment.Center,Tag=row };
            row.Children.Add(bar);waveBars.Add(bar);
        }
        return row;
    }
    static void AnimateWave() {
        wavePhase+=0.21;
        if(compactRecord!=null)
            compactRecord.Opacity=.82+.18*Math.Abs(Math.Sin(wavePhase*1.5));
        for(int i=0;i<waveBars.Count;i++) {
            double pulse=Math.Abs(Math.Sin(wavePhase+i*0.66));
            double swell=Math.Abs(Math.Sin(wavePhase*0.43-i*0.32));
            waveBars[i].Height=4+Math.Round((pulse*0.7+swell*0.3)*19);
        }
    }
    static void UpdateWaveAnimation() {
        if(waveTick==null)return;
        bool active=recording && floatWindow!=null && floatWindow.IsVisible;
        if(active) {
            if(!waveTick.IsEnabled)waveTick.Start();
        } else {
            if(waveTick.IsEnabled)waveTick.Stop();
            if(compactRecord!=null)compactRecord.Opacity=1;
        }
        foreach(var bar in waveBars) {
            var row=bar.Tag as StackPanel;
            if(row!=null)row.Opacity=active?1:0;
            if(!active)bar.Height=4;
        }
    }
    static void ApplyGlass() {
        if (floatOuter == null) return;
        byte alpha = (byte)(75 + glassStrength * 1.5);
        var baseColor = darkTheme
            ? Color.FromArgb(alpha, 26, 28, 31)
            : Color.FromArgb(alpha, 249, 250, 248);
        floatOuter.Background = new SolidColorBrush(baseColor);
        if (glassValue != null)
            glassValue.Text = ((int)Math.Round(glassStrength)).ToString() + "%";
    }
}
