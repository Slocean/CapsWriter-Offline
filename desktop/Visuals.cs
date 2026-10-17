using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using Rectangle = System.Windows.Shapes.Rectangle;

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
    static bool autoCopy;
    static bool floatTopmost=true;
    static double glassStrength = 55;
    static double floatCompletionSeconds = 1;
    static double floatLeft = Double.NaN, floatTop = Double.NaN;
    static string UiPrefsPath { get { return Path.Combine(Dir, "desktop_ui.ini"); } }
    static Button themeButton, floatStart, floatCancel;
    static TextBlock glassValue, completionValue;
    static Border floatOuter;
    static System.Windows.Threading.DispatcherTimer waveTick, floatCompleteTick;
    static readonly List<Rectangle> waveBars = new List<Rectangle>();
    static UdpClient meterSocket;
    enum FloatState { Idle, Listening, Recognizing, Complete }
    static FloatState floatState;
    static bool meterSupported, floatCompleted, floatCancelled;
    static double audioLevel;
    static DateTime meterReceived, meterRequested, waveFrame;
    static double waveOffset, recognitionPhase;
    static Grid floatWave;
    static Slider glassSlider, completionSlider;
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

    // 主界面只使用灰白色阶；浮窗沿用独立配色。
    // 文字层级：标题 > 小节标题 > 次要 > 辅助说明
    static TextBlock SectionTitle(string value) { return Text(value,15,"#262626","#F4F4F4",true); }
    static TextBlock Hint(string value) { return Text(value,13,"#737373","#B3B3B3"); }
    static TextBlock FieldLabel(string value) { return Text(value,13,"#666666","#C2C2C2"); }
    // 表面与控件底色
    static Brush PageBg() { return T("#FFFFFF","#262626"); }
    static Brush CardBg() { return T("#FFFFFF","#262626"); }
    static Brush CardLine() { return T("#E8E8E8","#444444"); }
    static Brush ChipBg() { return T("#F3F3F3","#333333"); }
    static Brush FieldBg() { return T("#FFFFFF","#303030"); }
    static Brush FieldLine() { return T("#DEDEDE","#505050"); }
    static Brush FieldInk() { return T("#262626","#F4F4F4"); }
    // 语义状态色（连接/录音/告警），低饱和
    static string OkHex { get { return darkTheme?"#6FBC92":"#5FA982"; } }
    static string RecHex { get { return darkTheme?"#E5837C":"#D96A64"; } }
    static string WarnHex { get { return darkTheme?"#D4A459":"#D99A4E"; } }
    static string IdleHex { get { return darkTheme?"#8D9094":"#A9ACAF"; } }

    static void ApplyTheme() {
        foreach (var ink in inks.Values) ink.Refresh();
        if (themeButton != null) themeButton.Content=IconLabel(darkTheme?"sun":"moon",darkTheme?"白色":"灰色");
        ApplyGlass();
    }
    static TextBlock Text(string value, int size, string light, string dark, bool bold=false) {
        return new TextBlock { Text=value, FontSize=size, Foreground=T(light,dark),
            FontWeight=bold ? FontWeights.Medium : FontWeights.Normal,
            VerticalAlignment=VerticalAlignment.Center };
    }
    static Button ThemeButton(string title, string lightBg, string darkBg, string lightFg,
                              string darkFg, double radius=10) {
        var button=Btn(title,lightBg,lightFg,radius);
        button.Background=T(lightBg,darkBg);
        button.Foreground=T(lightFg,darkFg);
        return button;
    }
    static Brush MutedInk() { return T("#737373","#B3B3B3"); }
    static Brush AccentInk() { return T("#262626","#F2F2F2"); }
    static Brush ActionFill() { return T("#262626","#F2F2F2"); }
    static Brush ActionInk() { return T("#FFFFFF","#262626"); }
    static Button ActionButton(string title,bool primary=false) {
        var button=ThemeButton(title,primary?"#262626":"#F4F4F4",primary?"#F2F2F2":"#383838",
            primary?"#FFFFFF":"#4D4D4D",primary?"#262626":"#D9D9D9",9);
        button.Height=40;button.FontWeight=FontWeights.Medium;button.Padding=new Thickness(14,0,14,0);return button;
    }
    static ControlTemplate InputTemplate(Type type) {
        var border=new FrameworkElementFactory(typeof(Border));border.Name="InputBorder";
        border.SetValue(Border.CornerRadiusProperty,new CornerRadius(8));
        border.SetBinding(Border.BackgroundProperty,new Binding("Background") { RelativeSource=new RelativeSource(RelativeSourceMode.TemplatedParent) });
        border.SetBinding(Border.BorderBrushProperty,new Binding("BorderBrush") { RelativeSource=new RelativeSource(RelativeSourceMode.TemplatedParent) });
        border.SetBinding(Border.BorderThicknessProperty,new Binding("BorderThickness") { RelativeSource=new RelativeSource(RelativeSourceMode.TemplatedParent) });
        var host=new FrameworkElementFactory(typeof(ScrollViewer));host.Name="PART_ContentHost";
        host.SetValue(UIElement.FocusableProperty,false);
        border.AppendChild(host);
        var template=new ControlTemplate(type) { VisualTree=border };
        var focus=new Trigger { Property=UIElement.IsKeyboardFocusWithinProperty,Value=true };
        focus.Setters.Add(new Setter(Border.BorderBrushProperty,FieldInk(),"InputBorder"));
        template.Triggers.Add(focus);return template;
    }
    static ControlTemplate ThinSliderTemplate() {
        const string xaml=@"<ControlTemplate xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'
 xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml' TargetType='{x:Type Slider}'>
 <Grid Background='Transparent' Height='32'>
  <Grid.Resources>
   <ControlTemplate x:Key='Line' TargetType='{x:Type RepeatButton}'>
    <Grid Background='Transparent'><Border Height='3' VerticalAlignment='Center' Background='{TemplateBinding Background}' CornerRadius='1.5'/></Grid>
   </ControlTemplate>
  </Grid.Resources>
  <Border x:Name='FocusRing' BorderBrush='{TemplateBinding Foreground}' BorderThickness='1' CornerRadius='6' Opacity='0' IsHitTestVisible='False'/>
  <Track x:Name='PART_Track' Margin='3,0' Minimum='{TemplateBinding Minimum}' Maximum='{TemplateBinding Maximum}'
   Value='{Binding Value, RelativeSource={RelativeSource TemplatedParent}, Mode=TwoWay}' Orientation='Horizontal'>
   <Track.DecreaseRepeatButton><RepeatButton Command='{x:Static Slider.DecreaseLarge}' Focusable='False' Background='{TemplateBinding Foreground}' Template='{StaticResource Line}'/></Track.DecreaseRepeatButton>
   <Track.IncreaseRepeatButton><RepeatButton Command='{x:Static Slider.IncreaseLarge}' Focusable='False' Background='{TemplateBinding Background}' Template='{StaticResource Line}'/></Track.IncreaseRepeatButton>
   <Track.Thumb><Thumb Width='22' Height='28' Background='{TemplateBinding Foreground}'>
    <Thumb.Template><ControlTemplate TargetType='{x:Type Thumb}'>
     <Grid Background='Transparent'><Ellipse Width='14' Height='14' Fill='{TemplateBinding Background}'/></Grid>
    </ControlTemplate></Thumb.Template>
   </Thumb></Track.Thumb>
  </Track>
 </Grid>
 <ControlTemplate.Triggers><Trigger Property='IsKeyboardFocusWithin' Value='True'><Setter TargetName='FocusRing' Property='Opacity' Value='.3'/></Trigger></ControlTemplate.Triggers>
</ControlTemplate>";
        return (ControlTemplate)System.Windows.Markup.XamlReader.Parse(xaml);
    }
    static FontFamily MainFont() {
        return new FontFamily(new Uri(Dir), "./assets/fonts/#Noto Sans SC");
    }
    static Border BrandMark() {
        string path=Path.Combine(Dir,"assets","brand-mark.png");
        var mark=new Border { Width=32,Height=32,Background=FieldInk(),IsHitTestVisible=false };
        if(File.Exists(path)) {
            var image=System.Windows.Media.Imaging.BitmapFrame.Create(new Uri(path));
            mark.OpacityMask=new ImageBrush(image) { Stretch=Stretch.Uniform };
        } else {
            mark.Background=Brushes.Transparent;mark.Child=MainIcon("voice");
        }
        System.Windows.Automation.AutomationProperties.SetName(mark,"CapsWriter 应用标志");
        return mark;
    }
    static System.Windows.Shapes.Path MainIcon(string kind) {
        string path=kind=="logs"?"M 5,4 L 17,4 M 5,10 L 17,10 M 5,16 L 13,16 M 1,4 L 1.2,4 M 1,10 L 1.2,10 M 1,16 L 1.2,16":
            kind=="settings"?"M 7,2 L 13,2 L 13,5 L 16,7 L 19,7 L 19,13 L 16,13 L 13,15 L 13,18 L 7,18 L 7,15 L 4,13 L 1,13 L 1,7 L 4,7 L 7,5 Z M 13,10 A 3,3 0 1 1 7,10 A 3,3 0 1 1 13,10":
            kind=="minimize"?"M 5,10.5 L 15,10.5":
            kind=="maximize"?"M 6.5,5.5 L 13.5,5.5 Q 14.5,5.5 14.5,6.5 L 14.5,13.5 Q 14.5,14.5 13.5,14.5 L 6.5,14.5 Q 5.5,14.5 5.5,13.5 L 5.5,6.5 Q 5.5,5.5 6.5,5.5 Z":
            kind=="restore"?"M 7.5,4.5 L 14.5,4.5 Q 15.5,4.5 15.5,5.5 L 15.5,12.5 M 5.5,7.5 L 11.5,7.5 Q 12.5,7.5 12.5,8.5 L 12.5,14.5 Q 12.5,15.5 11.5,15.5 L 5.5,15.5 Q 4.5,15.5 4.5,14.5 L 4.5,8.5 Q 4.5,7.5 5.5,7.5 Z":
            kind=="close"?"M 6,6 L 14,14 M 14,6 L 6,14":
            kind=="moon"?"M 16,12 A 7,7 0 1 1 8,3 A 6,6 0 0 0 16,12 Z":
            "M 13,10 A 3,3 0 1 1 7,10 A 3,3 0 1 1 13,10 M 10,1 L 10,3 M 10,17 L 10,19 M 1,10 L 3,10 M 17,10 L 19,10 M 4,4 L 5,5 M 15,15 L 16,16 M 4,16 L 5,15 M 15,5 L 16,4";
        var icon=kind=="voice"?FloatGlyph("microphone"):new System.Windows.Shapes.Path { Data=Geometry.Parse(path) };
        icon.Width=18;icon.Height=18;icon.Stretch=Stretch.Uniform;icon.Stroke=MutedInk();icon.StrokeThickness=1.6;
        icon.StrokeStartLineCap=icon.StrokeEndLineCap=PenLineCap.Round;icon.StrokeLineJoin=PenLineJoin.Round;
        // Fixed icon coordinates keep the single-line minimize glyph from stretching.
        if(kind=="minimize" || kind=="maximize" || kind=="restore" || kind=="close") {
            icon.Width=20;icon.Height=20;icon.Stretch=Stretch.None;icon.StrokeThickness=1.25;
            icon.Stroke=FieldInk();
        }
        icon.HorizontalAlignment=HorizontalAlignment.Center;icon.VerticalAlignment=VerticalAlignment.Center;
        icon.IsHitTestVisible=false;return icon;
    }
    static StackPanel IconLabel(string icon,string title) {
        var row=new StackPanel { Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center };
        var glyph=MainIcon(icon);
        BindingOperations.SetBinding(glyph,System.Windows.Shapes.Shape.StrokeProperty,new Binding("Foreground") {
            RelativeSource=new RelativeSource(RelativeSourceMode.FindAncestor,typeof(Button),1) });
        row.Children.Add(glyph);
        row.Children.Add(new TextBlock { Text=title,Margin=new Thickness(11,0,0,0),VerticalAlignment=VerticalAlignment.Center });
        return row;
    }
    static Button NavigationButton(string title) {
        var button=ActionButton(title);button.Width=84;button.Height=44;button.Padding=new Thickness(10,0,10,0);
        button.Template=ButtonTemplate(0);button.Margin=new Thickness(3,0,3,0);
        System.Windows.Automation.AutomationProperties.SetName(button,title);
        System.Windows.Shell.WindowChrome.SetIsHitTestVisibleInChrome(button,true);return button;
    }
    static Button ChromeButton(string icon,string title) {
        var button=ActionButton("");button.Content=MainIcon(icon);button.Background=Brushes.Transparent;
        button.Width=36;button.Height=36;button.Padding=new Thickness(0);button.ToolTip=title;
        System.Windows.Automation.AutomationProperties.SetName(button,title);
        System.Windows.Shell.WindowChrome.SetIsHitTestVisibleInChrome(button,true);return button;
    }
    static StackPanel PageHeading(string title,string description) {
        var heading=new StackPanel();heading.Children.Add(Text(title,32,"#262626","#F4F4F4",true));
        var detail=Hint(description);detail.Margin=new Thickness(0,7,0,0);detail.TextWrapping=TextWrapping.Wrap;
        heading.Children.Add(detail);return heading;
    }
    static Border SettingsSection(string title,string description,UIElement content) {
        var section=new StackPanel();section.Children.Add(SectionTitle(title));
        var detail=Hint(description);detail.TextWrapping=TextWrapping.Wrap;detail.Margin=new Thickness(0,7,0,17);
        section.Children.Add(detail);section.Children.Add(content);
        return new Border { BorderBrush=CardLine(),BorderThickness=new Thickness(0,0,0,1),
            Padding=new Thickness(0,0,0,28),Margin=new Thickness(0,0,0,28),Child=section };
    }
    static void SetSegmentVisual(Button button,bool selected) {
        button.Background=selected?T("#FFFFFF","#4A4A4A"):Brushes.Transparent;
        button.Foreground=selected?AccentInk():MutedInk();
        button.BorderBrush=Brushes.Transparent;button.BorderThickness=new Thickness(0);
        System.Windows.Automation.AutomationProperties.SetItemStatus(button,selected?"已选择":"");
    }
    static void UpdateModeButtons() {
        if (liveModeButton==null || batchModeButton==null) return;
        SetSegmentVisual(liveModeButton,liveMode);SetSegmentVisual(batchModeButton,!liveMode);
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
       <Border Background='#A3A3A3' CornerRadius='4' Margin='2,1'/>
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
                else if (key=="auto_copy") autoCopy=value=="1";
                else if (key=="float_topmost") floatTopmost=value=="1";
                else if (key=="completion_seconds" && Double.TryParse(value,NumberStyles.Float,CultureInfo.InvariantCulture,out n)
                    && !Double.IsNaN(n) && !Double.IsInfinity(n))
                    floatCompletionSeconds=Math.Max(.5,Math.Min(10,n));
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
                "auto_copy="+(autoCopy?"1":"0")+"\n"+
                "float_topmost="+(floatTopmost?"1":"0")+"\n"+
                "completion_seconds="+floatCompletionSeconds.ToString("0.0",CultureInfo.InvariantCulture)+"\n"+
                "left="+floatLeft.ToString("F0",CultureInfo.InvariantCulture)+"\n"+
                "top="+floatTop.ToString("F0",CultureInfo.InvariantCulture)+"\n";
            File.WriteAllText(UiPrefsPath,data);
        } catch (IOException) {} catch (UnauthorizedAccessException) {}
    }
    static void UpdateCompletionSetting() {
        if(completionValue!=null)completionValue.Text=floatCompletionSeconds.ToString("0.#",CultureInfo.InvariantCulture)+" 秒";
        if(floatCompleteTick!=null && floatCompleteTick.IsEnabled)
            floatCompleteTick.Interval=TimeSpan.FromSeconds(floatCompletionSeconds);
    }
    static void UpdateFloatLayout() {
        if(floatWindow==null || floatStart==null)return;
        var next=recording?FloatState.Listening:floatCancelled?FloatState.Idle:
            processing?FloatState.Recognizing:floatCompleted?FloatState.Complete:FloatState.Idle;
        if(next!=floatState) {
            SetFloatButtonVisual(floatStart,next==FloatState.Complete?"confirm":"microphone");
            floatCompleteTick.Stop();
            if(next==FloatState.Complete){
                floatCompleteTick.Interval=TimeSpan.FromSeconds(floatCompletionSeconds);floatCompleteTick.Start();
            }
            waveOffset=0;recognitionPhase=0;waveFrame=DateTime.UtcNow;
            foreach(var bar in waveBars) {
                bar.Height=2;
                ((TranslateTransform)((StackPanel)bar.Tag).RenderTransform).X=0;
            }
        }
        floatState=next;
        bool listening=next==FloatState.Listening, recognizing=next==FloatState.Recognizing;
        floatCancel.Visibility=floatRecord.Visibility=listening?Visibility.Visible:Visibility.Collapsed;
        floatWave.Visibility=listening || recognizing?Visibility.Visible:Visibility.Collapsed;
        floatStart.Visibility=listening || recognizing?Visibility.Collapsed:Visibility.Visible;
        floatWave.Width=recognizing?70:90;
        for(int i=0;i<waveBars.Count;i++)waveBars[i].Visibility=recognizing && i>=14?Visibility.Collapsed:Visibility.Visible;
        floatWindow.Width=next==FloatState.Idle?56:220;
        floatWindow.Height=56;
        floatWindow.SizeToContent=SizeToContent.Manual;
        floatWindow.UpdateLayout();
        var work=SystemParameters.WorkArea;
        var source=PresentationSource.FromVisual(floatWindow);
        if(source!=null && source.CompositionTarget!=null) {
            var area=System.Windows.Forms.Screen.FromHandle(
                new System.Windows.Interop.WindowInteropHelper(floatWindow).Handle).WorkingArea;
            var transform=source.CompositionTarget.TransformFromDevice;
            work=new Rect(transform.Transform(new Point(area.Left,area.Top)),
                transform.Transform(new Point(area.Right,area.Bottom)));
        }
        floatWindow.Left=Math.Max(work.Left,Math.Min(floatWindow.Left,work.Right-floatWindow.Width));
        double height=floatWindow.ActualHeight>0?floatWindow.ActualHeight:floatWindow.Height;
        floatWindow.Top=Math.Max(work.Top,Math.Min(floatWindow.Top,work.Bottom-height));
        ApplyGlass();
    }
    static bool ApplyFloatTopmost() {
        if(floatWindow==null)return true;
        floatWindow.Topmost=floatTopmost;
        var handle=new System.Windows.Interop.WindowInteropHelper(floatWindow).Handle;
        if(handle==IntPtr.Zero)return true;
        // Reassert z-order even when WPF's property value has not changed; keep input focus.
        return SetWindowPos(handle,new IntPtr(floatTopmost?-1:-2),0,0,0,0,0x0013);
    }
    static System.Windows.Shapes.Path FloatGlyph(string kind) {
        return new System.Windows.Shapes.Path {
            Data=Geometry.Parse(kind=="cancel"?"M 5,5 L 15,15 M 15,5 L 5,15":
                kind=="confirm"?"M 4,10 L 8,14 L 16,6":
                "M 7,4 A 3,3 0 0 1 13,4 L 13,10 A 3,3 0 0 1 7,10 Z M 4,9 L 4,10 A 6,6 0 0 0 16,10 L 16,9 M 10,16 L 10,19 M 7,19 L 13,19"),
            Width=20,Height=20,Stretch=Stretch.None,StrokeThickness=2,
            Stroke=T("#33363A","#E7E8E6"),StrokeStartLineCap=PenLineCap.Round,
            StrokeEndLineCap=PenLineCap.Round,StrokeLineJoin=PenLineJoin.Round };
    }
    static Button FloatButton(string kind) {
        var button=ThemeButton("","#00000000","#00000000","#33363A","#E7E8E6",22);
        button.Width=44;button.Height=44;button.Padding=new Thickness(0);
        SetFloatButtonVisual(button,kind);
        button.ToolTip=kind=="cancel"?"取消录音":kind=="confirm"?"结束录音并识别":"点击开始录音";
        System.Windows.Automation.AutomationProperties.SetName(button,(string)button.ToolTip);
        ToolTipService.SetShowOnDisabled(button,true);
        return button;
    }
    static void SetFloatButtonVisual(Button button,string kind) {
        bool confirm=kind=="confirm";
        button.Background=T(confirm?"#26282B":kind=="cancel"?"#11000000":"#00000000",
            confirm?"#EDEEEC":kind=="cancel"?"#22FFFFFF":"#00000000");
        button.BorderBrush=confirm?T("#26282B","#EDEEEC"):Brushes.Transparent;
        button.BorderThickness=new Thickness(confirm?1:0);
        var glyph=FloatGlyph(kind);
        if(confirm)glyph.Stroke=T("#FFFFFF","#1D1F22");
        button.Content=glyph;
    }
    static Grid WaveBars(int count) {
        var row=new StackPanel { Orientation=Orientation.Horizontal,
            Height=32,RenderTransform=new TranslateTransform() };
        for(int i=0;i<count;i++) {
            var bar=new Rectangle { Width=2,Height=2,RadiusX=1,RadiusY=1,
                Fill=T("#33363A","#E7E8E6"),Margin=new Thickness(1.5,0,1.5,0),
                VerticalAlignment=VerticalAlignment.Center,Tag=row };
            row.Children.Add(bar);waveBars.Add(bar);
        }
        var viewport=new Grid { Width=count*5,Height=32,ClipToBounds=true,
            VerticalAlignment=VerticalAlignment.Center };
        viewport.Children.Add(row);
        return viewport;
    }
    static bool ApplyMeterReply(string reply) {
        double level;
        if(reply==null || !reply.StartsWith("LEVEL|",StringComparison.Ordinal) ||
            !Double.TryParse(reply.Substring(6),NumberStyles.Float,CultureInfo.InvariantCulture,out level) ||
            Double.IsNaN(level) || Double.IsInfinity(level) || level<0 || level>1)return false;
        audioLevel=level;meterReceived=DateTime.UtcNow;
        return true;
    }
    static void ReadAudioMeter() {
        if(!meterSupported)return;
        try {
            if(meterSocket==null) {
                meterSocket=new UdpClient();
                meterSocket.Connect(IPAddress.Loopback,6018);
            }
            while(meterSocket.Available>0) {
                var remote=new IPEndPoint(IPAddress.Loopback,0);
                ApplyMeterReply(Encoding.ASCII.GetString(meterSocket.Receive(ref remote)));
            }
            if((DateTime.UtcNow-meterRequested).TotalMilliseconds>=50) {
                var request=Encoding.ASCII.GetBytes("LEVEL");
                meterSocket.Send(request,request.Length);
                meterRequested=DateTime.UtcNow;
            }
        } catch(SocketException) { audioLevel=0; }
    }
    static void AdvanceWave(double level, double elapsedMs) {
        waveOffset+=Math.Max(0,Math.Min(100,elapsedMs))*.03;
        while(waveOffset>=5) {
            for(int i=0;i<waveBars.Count-1;i++)waveBars[i].Height=waveBars[i+1].Height;
            if(waveBars.Count>0)
                waveBars[waveBars.Count-1].Height=2+26.16*(level<.0025?0:Math.Min(1,level*8));
            waveOffset-=5;
        }
        for(int i=0;i<waveBars.Count;i++) {
            double edge=Math.Min(1,Math.Min(i,waveBars.Count-1-i)*5/20.0);
            waveBars[i].Opacity=.45*(waveBars[i].Height>2?1:.5)*edge*edge*(3-2*edge);
            ((TranslateTransform)((StackPanel)waveBars[i].Tag).RenderTransform).X=-waveOffset;
        }
    }
    static void AnimateWave() {
        var now=DateTime.UtcNow;
        double elapsed=(now-waveFrame).TotalMilliseconds;
        if(recording) {
            ReadAudioMeter();
            AdvanceWave((now-meterReceived).TotalMilliseconds<250?audioLevel:0,elapsed);
        } else if(floatState==FloatState.Recognizing) {
            recognitionPhase+=Math.Max(0,Math.Min(100,elapsed))*.003;
            for(int i=0;i<waveBars.Count;i++) {
                waveBars[i].Height=4+22*(1-i/18.0)*(.55+.45*Math.Pow(Math.Sin(recognitionPhase-i*.24),2));
                waveBars[i].Opacity=1;
                ((TranslateTransform)((StackPanel)waveBars[i].Tag).RenderTransform).X=0;
            }
        }
        waveFrame=now;
    }
    static void UpdateWaveAnimation() {
        if(waveTick==null)return;
        bool active=(floatState==FloatState.Listening || floatState==FloatState.Recognizing) && floatWindow!=null && floatWindow.IsVisible;
        if(active) {
            if(!waveTick.IsEnabled){waveFrame=DateTime.UtcNow;waveTick.Start();}
        } else {
            if(waveTick.IsEnabled)waveTick.Stop();
        }
        if(!active || !recording) {
            if(meterSocket!=null){meterSocket.Close();meterSocket=null;}
            audioLevel=0;meterReceived=meterRequested=DateTime.MinValue;
        }
        foreach(var bar in waveBars) {
            var row=bar.Tag as StackPanel;
            if(row!=null)row.Opacity=1;
            if(!active){bar.Height=2;((TranslateTransform)row.RenderTransform).X=0;}
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
