using System;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;

// Shadows the clipboard only in this test assembly; never changes the user's clipboard.
internal static class Clipboard {
    internal static string Value;
    internal static int Writes;
    internal static bool Busy;
    public static void SetDataObject(object value,bool persist) {
        if(Busy)throw new System.Runtime.InteropServices.ExternalException("busy");
        Value=(string)value;Writes++;
    }
}
internal static class DesktopFeatureTests {
    [STAThread] static int Main() {
        try { Desktop.CheckResultOptions();return 0; }
        catch(Exception error) {
            Console.Error.WriteLine(error.GetType().FullName+": "+error.Message);
            Console.Error.WriteLine(error.StackTrace);return 1;
        }
    }
}
internal static partial class Desktop {
    static void Check(bool value,string message) { if(!value)throw new Exception(message); }
    static CheckBox FindSwitch(DependencyObject root,string title) {
        var box=root as CheckBox;
        if(box!=null && (string)box.Content==title)return box;
        foreach(object child in LogicalTreeHelper.GetChildren(root)) {
            var node=child as DependencyObject;
            if(node==null)continue;
            var found=FindSwitch(node,title);
            if(found!=null)return found;
        }
        return null;
    }
    static string TextEvent(string value) {
        return "12:34:56.789 INFO  [ result_processor.py:322] 任务生命周期: 文字 "+
            new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(value);
    }
    internal static void CheckResultOptions() {
        LoadUiPrefs();
        Check(!autoCopy && floatTopmost,"defaults");
        BuildMain();BuildFloat();
        var copy=FindSwitch(main,"识别完成后自动复制到剪贴板");
        var pin=FindSwitch(main,"浮窗始终置于最顶层");
        Check(copy!=null && pin!=null,"settings switches exist");
        Check(transcript.IsReadOnly && transcript.VerticalScrollBarVisibility==ScrollBarVisibility.Auto,"selectable scrolling result");
        Check(floatWindow.Topmost,"default topmost");
        pin.IsChecked=false;
        Check(!floatWindow.Topmost,"disable topmost immediately");
        copy.IsChecked=true;
        autoCopy=false;floatTopmost=true;LoadUiPrefs();
        Check(autoCopy && !floatTopmost,"persisted switches");
        pin.IsChecked=true;
        Check(floatWindow.Topmost,"enable topmost immediately");
        var handle=new WindowInteropHelper(floatWindow).EnsureHandle();
        Check((GetWindowLong(handle,-20)&8)!=0,"native HWND is topmost without activating");
        Check(SetWindowPos(handle,new IntPtr(-2),0,0,0,0,0x0013),"simulate native z-order loss");
        Check(floatWindow.Topmost && (GetWindowLong(handle,-20)&8)==0,"WPF property alone can drift from native state");
        Check(ApplyFloatTopmost() && (GetWindowLong(handle,-20)&8)!=0,"repair native z-order without changing preference");
        pin.IsChecked=false;
        Check((GetWindowLong(handle,-20)&8)==0 && !floatWindow.ShowActivated,"disable native topmost, keep no-activate");
        pin.IsChecked=true;
        // Legacy preference files retain defaults for the two newly added keys.
        File.WriteAllText(UiPrefsPath,"theme=dark\nglass=60\n");
        autoCopy=false;floatTopmost=true;LoadUiPrefs();
        Check(!autoCopy && floatTopmost && darkTheme && glassStrength==60,"legacy preferences");
        copy.IsChecked=false;
        ProcessLine("触发：开始录音");
        ProcessLine(TextEvent("关闭时不复制"));
        Check(Clipboard.Writes==0,"disabled leaves clipboard alone");
        copy.IsChecked=true;
        ProcessLine("触发：开始录音");
        ProcessLine("实时识别片段: 草稿");
        ProcessLine("收到最终识别结果: 原始文字, 时延: 0.2s");
        Check(Clipboard.Writes==0,"no copying interim or raw results");
        string first=new string('字',250)+"\n引号\"与\\路径";
        ProcessLine(TextEvent(first));
        Check(lastText==first && Clipboard.Value==first && Clipboard.Writes==1,"full processed result");
        ProcessLine("实时识别片段: 第二段草稿");
        Check(lastText==first+"第二段草稿","interim retains completed segments");
        ProcessLine(TextEvent("第二段"));
        Check(lastText==first+"第二段" && Clipboard.Value==lastText,"no duplicate interim; cumulative copy");
        ProcessLine(TextEvent("历史"),true);
        ProcessLine(TextEvent("   "));
        ProcessLine("任务生命周期: 文字 malformed");
        Check(Clipboard.Writes==2 && lastText==first+"第二段","history, empty and malformed ignored");
        ProcessLine("收到最终识别结果: 任务生命周期: 文字 \"伪造事件\", 时延: 0.2s");
        Check(Clipboard.Writes==2,"raw content cannot impersonate final event");
        using(tray=new System.Windows.Forms.NotifyIcon()) {
            UpdateDisplay();
            Check(transcript.Text==lastText,"result is not truncated at 160 characters");
            Check(floatTranscript.Text==lastText && floatTranscript.TextWrapping==TextWrapping.Wrap,
                "full result below waveform, without 28-character clipping");
            Check(copyResult.IsEnabled && DockPanel.GetDock(copyResult)==Dock.Right,"copy button at heading right");
            transcript.Select(3,10);
            UpdateDisplay();UpdateDisplay();
            Check(transcript.SelectionStart==3 && transcript.SelectionLength==10,"ticks preserve selection");
            Check(transcript.SelectedText==first.Substring(3,10),"selected text available for copy");
        }
        liveMode=false;
        ProcessLine(TextEvent("整段结果"));
        Check(lastText=="整段结果" && Clipboard.Value==lastText,"batch replaces previous result");
        Clipboard.Busy=true;
        ProcessLine(TextEvent("被占用也保留文字"));
        Check(lastText=="被占用也保留文字" && logBox.Text.Contains("自动复制失败"),"busy clipboard is recoverable");
        Clipboard.Busy=false;autoCopy=false;
        lastText=first+"第二段";
        transcript.Select(1,2);
        int writes=Clipboard.Writes;
        copyResult.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Check(Clipboard.Writes==writes+1 && Clipboard.Value==lastText && (string)copyResult.Content=="已复制",
            "manual button copies all text regardless of selection or automatic-copy setting");
        Clipboard.Busy=true;
        copyResult.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Check(Clipboard.Writes==writes+1 && lastText==first+"第二段","manual clipboard failure preserves result");
        Clipboard.Busy=false;lastText="";
        CopyResult(false);
        Check(Clipboard.Writes==writes+1,"empty placeholder never copied");
        CheckWaveform();
        Console.WriteLine("PASS: full-text copy button, native topmost repair, microphone waveform, floating transcript and preferences");
    }
    static void CheckWaveform() {
        Check(waveBars.Count==56 && waveBars[0].Width==2,"thin waveform geometry");
        AdvanceWave(.05,100);
        double spoken=waveBars[waveBars.Count-1].Height;
        Check(spoken>2,"speech increases waveform height");
        AdvanceWave(0,100);
        Check(waveBars[waveBars.Count-1].Height==2 && waveBars[waveBars.Count-2].Height==spoken,
            "silent samples return to baseline while speech scrolls left");
        AdvanceWave(1,100);
        Check(waveBars[waveBars.Count-1].Height<=28.16,"loud samples stay inside waveform");
        Check(ApplyMeterReply("LEVEL|0.025") && audioLevel==.025,"valid microphone RMS");
        foreach(string invalid in new[]{"LEVEL|NaN","LEVEL|Infinity","LEVEL|-1","LEVEL|2","LEVEL|bad","STATE|1"})
            Check(!ApplyMeterReply(invalid) && audioLevel==.025,"reject invalid meter reply: "+invalid);
        using(var server=new UdpClient(new IPEndPoint(IPAddress.Loopback,0))) {
            server.Client.ReceiveTimeout=1000;
            meterSocket=new UdpClient();meterSocket.Connect((IPEndPoint)server.Client.LocalEndPoint);
            meterSupported=true;meterRequested=DateTime.MinValue;
            ReadAudioMeter();
            var sender=new IPEndPoint(IPAddress.Loopback,0);
            Check(Encoding.ASCII.GetString(server.Receive(ref sender))=="LEVEL","meter uses read-only protocol");
            var reply=Encoding.ASCII.GetBytes("LEVEL|0.075");server.Send(reply,reply.Length,sender);
            Check(meterSocket.Client.Poll(1000000,System.Net.Sockets.SelectMode.SelectRead),"isolated UDP reply arrived");
            ReadAudioMeter();Check(audioLevel==.075,"actual UDP meter reply reaches waveform");
            meterSocket.Close();meterSocket=null;
        }
        ApplyStateReply("STATE|recording=1|connected=1|started=1|meter=1");
        Check(meterSupported,"new backend advertises meter support");
        ApplyStateReply("STATE|recording=1|connected=1|started=1");
        Check(!meterSupported,"old backend does not receive unsupported high-frequency requests");
        meterReceived=DateTime.UtcNow.AddSeconds(-1);waveFrame=DateTime.UtcNow.AddMilliseconds(-100);
        AnimateWave();Check(waveBars[waveBars.Count-1].Height==2,"stale audio falls back to silence");
        floatCollapsed=true;recording=true;UpdateFloatLayout();
        Check(expandedFloat.Visibility==Visibility.Visible && floatWindow.Width==360,"recording expands compact window");
        recording=false;floatResultVisible=true;UpdateFloatLayout();
        Check(expandedFloat.Visibility==Visibility.Visible,"recognized text stays visible after recording");
        SetFloatCollapsed(true);
        Check(compactFloat.Visibility==Visibility.Visible && floatWindow.Width==116,"manual collapse still works");
        SetFloatCollapsed(false);recording=true;connected=true;started=DateTime.Now;
        backend=System.Diagnostics.Process.GetCurrentProcess();
        lastText="这是语音输入的识别文字。\n声波随麦克风音量变化，识别结果显示在下方。";
        ApplyTheme();
        using(tray=new System.Windows.Forms.NotifyIcon())UpdateDisplay();
        for(int i=0;i<56;i++)AdvanceWave(i%11<7?.02+(i%7)*.012:0,84);
        string preview=Environment.GetEnvironmentVariable("CAPSWRITER_UI_PREVIEW_DIR");
        if(!String.IsNullOrEmpty(preview)) {
            Directory.CreateDirectory(preview);
            RenderPreview(floatOuter,360,System.IO.Path.Combine(preview,"recording.png"));
            RenderPreview((FrameworkElement)transcript.Parent,500,System.IO.Path.Combine(preview,"copy-result.png"));
        }
        recording=false;backend=null;UpdateWaveAnimation();
        foreach(var bar in waveBars)Check(bar.Height==2,"recording stop resets waveform");
    }
    static void RenderPreview(FrameworkElement element,int width,string path) {
        element.Measure(new Size(width,Double.PositiveInfinity));
        element.Arrange(new Rect(0,0,width,element.DesiredSize.Height));element.UpdateLayout();
        var image=new RenderTargetBitmap(width,(int)Math.Ceiling(element.ActualHeight),96,96,PixelFormats.Pbgra32);
        image.Render(element);
        var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(image));
        using(var file=File.Create(path))encoder.Save(file);
    }
}
