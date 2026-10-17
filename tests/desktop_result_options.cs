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
        Check(!autoCopy && floatTopmost && floatCompletionSeconds==1,"defaults include one-second completion");
        BuildMain();BuildFloat();
        var copy=FindSwitch(main,"识别完成后自动复制到剪贴板");
        var pin=FindSwitch(main,"浮窗始终置于最顶层");
        Check(copy!=null && pin!=null,"settings switches exist");
        Check(completionSlider.Value==1 && completionValue.Text=="1 秒","completion duration setting defaults to one second");
        completionSlider.Value=2.5;
        floatCompletionSeconds=1;LoadUiPrefs();
        Check(floatCompletionSeconds==2.5,"completion duration persists");
        completionSlider.Value=1;
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
        Check(!autoCopy && floatTopmost && darkTheme && glassStrength==60 && floatCompletionSeconds==1,"legacy preferences retain one-second default");
        string[] durations={"NaN","Infinity","bad","-1","99","2.5"};
        double[] expected={1,1,1,.5,10,2.5};
        for(int i=0;i<durations.Length;i++) {
            File.WriteAllText(UiPrefsPath,"completion_seconds="+durations[i]+"\n");
            floatCompletionSeconds=1;LoadUiPrefs();
            Check(floatCompletionSeconds==expected[i],"invalid completion duration cannot break timer");
        }
        floatCompletionSeconds=1;
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
        Console.WriteLine("PASS: full-text copy, native topmost, slower waveform, completion style/timer/preferences and lifecycle transitions");
    }
    static void CheckWaveform() {
        Check(waveBars.Count==18 && waveBars[0].Width==2,"thin waveform geometry");
        waveOffset=0;AdvanceWave(.05,100);
        Check(waveBars[waveBars.Count-1].Height==2 && Math.Abs(waveOffset-3)<.01,"wave scrolls at 30px per second, half its prior speed");
        AdvanceWave(.05,100);
        double spoken=waveBars[waveBars.Count-1].Height;
        Check(spoken>2,"speech increases waveform height");
        AdvanceWave(0,100);AdvanceWave(0,100);
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
        meterReceived=DateTime.UtcNow.AddSeconds(-1);
        for(int i=0;i<2;i++){waveFrame=DateTime.UtcNow.AddMilliseconds(-100);AnimateWave();}
        Check(waveBars[waveBars.Count-1].Height==2,"stale audio falls back to silence at the slower scroll speed");
        CheckFloatStates();CheckFloatCompletionTimeline();
        recording=true;connected=true;started=DateTime.Now;
        backend=System.Diagnostics.Process.GetCurrentProcess();
        lastText="这是语音输入的识别文字。\n声波随麦克风音量变化，识别结果显示在下方。";
        ApplyTheme();
        using(tray=new System.Windows.Forms.NotifyIcon())UpdateDisplay();
        for(int i=0;i<18;i++)AdvanceWave(i%11<7?.02+(i%7)*.012:0,84);
        string preview=Environment.GetEnvironmentVariable("CAPSWRITER_UI_PREVIEW_DIR");
        if(!String.IsNullOrEmpty(preview)) {
            Directory.CreateDirectory(preview);
            RenderPreview(floatOuter,220,System.IO.Path.Combine(preview,"listening.png"));
            recording=false;pendingTasks.Add("preview");UpdateFloatLayout();
            waveFrame=DateTime.UtcNow.AddMilliseconds(-80);AnimateWave();
            RenderPreview(floatOuter,220,System.IO.Path.Combine(preview,"recognizing.png"));
            pendingTasks.Clear();floatCompleted=true;UpdateFloatLayout();
            RenderPreview(floatOuter,220,System.IO.Path.Combine(preview,"complete.png"));
            floatCompleted=false;UpdateFloatLayout();
            RenderPreview(floatOuter,56,System.IO.Path.Combine(preview,"idle.png"));
            RenderPreview((FrameworkElement)transcript.Parent,500,System.IO.Path.Combine(preview,"copy-result.png"));
        }
        recording=false;backend=null;UpdateWaveAnimation();
        foreach(var bar in waveBars)Check(bar.Height==2,"recording stop resets waveform");
    }
    static void CheckFloatStates() {
        pendingTasks.Clear();recording=false;floatCompleted=false;floatCancelled=false;awaitingAudio=false;
        UpdateFloatLayout();
        Check(floatState==FloatState.Idle && floatWindow.Width==56 && floatWindow.Height==56,"idle is a single microphone icon");
        Check(floatStart.Visibility==Visibility.Visible && floatWave.Visibility==Visibility.Collapsed,"idle has no wave or extra controls");
        var colors=new System.Windows.Media.Color[2];
        for(int theme=0;theme<2;theme++) {
            darkTheme=theme==1;ApplyTheme();colors[theme]=((SolidColorBrush)floatOuter.Background).Color;
            foreach(FloatState state in Enum.GetValues(typeof(FloatState))) {
                recording=state==FloatState.Listening;pendingTasks.Clear();
                if(state==FloatState.Recognizing)pendingTasks.Add("checking");
                floatCompleted=state==FloatState.Complete;
                UpdateFloatLayout();
                Check(floatState==state,"four states follow actual recording and task completion");
                Check(((SolidColorBrush)floatOuter.Background).Color==colors[theme],"background and opacity remain unchanged across states");
                Check(floatWindow.Height==56 && floatWindow.Width==(state==FloatState.Idle?56:220),"fixed capsule geometry without text row");
                bool listening=state==FloatState.Listening;
                Check((floatCancel.Visibility==Visibility.Visible)==listening &&
                    (floatRecord.Visibility==Visibility.Visible)==listening,"side controls appear only while listening");
                floatOuter.Measure(new Size(floatWindow.Width,56));
                floatOuter.Arrange(new Rect(0,0,floatWindow.Width,56));floatOuter.UpdateLayout();
                if(listening) {
                    var left=floatCancel.TransformToAncestor(floatOuter).TransformBounds(new Rect(0,0,44,44));
                    var right=floatRecord.TransformToAncestor(floatOuter).TransformBounds(new Rect(0,0,44,44));
                    var wave=floatWave.TransformToAncestor(floatOuter).TransformBounds(new Rect(0,0,floatWave.Width,32));
                    Check(left.Left>=0 && right.Right<=220 && left.Right<wave.Left && wave.Right<right.Left,
                        "44px controls and waveform fit without overlap");
                } else if(state==FloatState.Idle || state==FloatState.Complete) {
                    var icon=floatStart.TransformToAncestor(floatOuter).Transform(new Point(22,22));
                    Check(Math.Abs(icon.X-floatWindow.Width/2)<1 && Math.Abs(icon.Y-28)<1,"idle and completion icons stay centered");
                }
                if(state==FloatState.Complete) {
                    var done=(System.Windows.Shapes.Path)floatStart.Content;
                    var confirm=(System.Windows.Shapes.Path)floatRecord.Content;
                    Check(done.Data.ToString()==confirm.Data.ToString() &&
                        ((SolidColorBrush)done.Stroke).Color==((SolidColorBrush)confirm.Stroke).Color &&
                        ((SolidColorBrush)floatStart.Background).Color==((SolidColorBrush)floatRecord.Background).Color &&
                        floatStart.BorderThickness==floatRecord.BorderThickness && floatStart.BorderThickness.Left==1,
                        "completion uses the same filled bordered check button in both themes");
                    var border=(Border)floatStart.Template.FindName("Root",floatStart);
                    Check(border.CornerRadius.TopLeft==22 && border.BorderThickness.Left==1 &&
                        ((SolidColorBrush)border.Background).Color==((SolidColorBrush)floatRecord.Background).Color,
                        "actual rendered completion border has circular fill and outline");
                }
            }
        }
        Check(colors[0]!=colors[1],"existing light and dark palettes retained");
        pendingTasks.Clear();floatCompleted=false;recording=true;floatCancelled=false;
        ProcessLine("任务生命周期: 提交 first");ProcessLine("任务生命周期: 提交 second");
        ProcessLine(TextEvent("第一段"));UpdateFloatLayout();
        Check(floatState==FloatState.Listening,"a phrase result does not interrupt recording");
        ProcessLine("释放：完成录音");ProcessLine("任务生命周期: 完成 first");UpdateFloatLayout();
        Check(floatState==FloatState.Recognizing,"wait for every submitted segment");
        waveFrame=DateTime.UtcNow.AddMilliseconds(-80);AnimateWave();
        double firstHeight=waveBars[0].Height;
        waveFrame=DateTime.UtcNow.AddMilliseconds(-80);AnimateWave();
        Check(waveBars[0].Height!=firstHeight && floatWave.Width==70,"recognizing has a compact animated centered wave");
        ProcessLine("任务生命周期: 完成 second");UpdateFloatLayout();
        Check(floatState==FloatState.Complete,"completion check waits until pending tasks drain");
        ProcessLine("触发：开始录音");UpdateFloatLayout();
        Check(floatState==FloatState.Listening && !floatCompleted,"new recording clears prior completion");
        Check(!ApplyCancelReply(null) && recording,"missing cancel acknowledgement cannot fake success");
        Check(ApplyCancelReply("CANCELLED") && !recording,"confirmed cancellation ends listening");
        ProcessLine(TextEvent("取消后的在途结果"));UpdateFloatLayout();
        Check(floatState==FloatState.Idle && lastText.Contains("取消后的在途结果"),"late results after cancel stay in main text without a false check");
        ProcessLine("触发：开始录音");ProcessLine("释放：完成录音");UpdateFloatLayout();
        Check(floatState==FloatState.Idle,"empty recording cannot show a success check");
        ProcessLine("任务生命周期: 提交 failed");ProcessLine("任务生命周期: 发送失败 failed");UpdateFloatLayout();
        Check(floatState==FloatState.Idle,"failed recognition cannot show a success check");
        floatCompleted=true;ProcessLine("WebSocket 断开");UpdateFloatLayout();
        Check(floatState==FloatState.Idle,"disconnect cannot leave a stale completion check");
        floatCompleted=true;
        ProcessLine("历史开始 触发：开始录音",true);UpdateFloatLayout();
        Check(floatState==FloatState.Complete,"historical lines cannot change current completion");
        floatCompleted=false;pendingTasks.Clear();
    }
    static void PumpDispatcher(int milliseconds) {
        var frame=new System.Windows.Threading.DispatcherFrame();
        var stop=new System.Windows.Threading.DispatcherTimer { Interval=TimeSpan.FromMilliseconds(milliseconds) };
        stop.Tick+=(s,e)=>{stop.Stop();frame.Continue=false;};stop.Start();
        System.Windows.Threading.Dispatcher.PushFrame(frame);
    }
    static void CheckFloatCompletionTimeline() {
        pendingTasks.Clear();recording=false;floatCompleted=false;floatCancelled=false;awaitingAudio=false;
        connected=true;backend=System.Diagnostics.Process.GetCurrentProcess();
        using(tray=new System.Windows.Forms.NotifyIcon()) {
            ApplyStateReply("STATE|recording=0|connected=1|capture=1");
            ProcessLine("触发：开始录音");ProcessLine("任务生命周期: 录音开始 session-a");UpdateDisplay();
            Check(floatState==FloatState.Listening,"recording starts listening");
            ProcessLine("释放：完成录音");UpdateDisplay();
            Check(floatState==FloatState.Recognizing,"release-to-submit gap must never flash a microphone");
            ProcessLine("任务生命周期: 提交 final-a");ProcessLine("任务生命周期: 录音结束 session-a");UpdateDisplay();
            Check(floatState==FloatState.Recognizing,"submitted audio stays recognizing");
            ProcessLine("收到最终识别结果: 原始结果, 时延: 0.2s");UpdateDisplay();
            Check(floatState==FloatState.Recognizing,"raw result does not end postprocessing waiting");
            ProcessLine(TextEvent("已处理的结果"));UpdateDisplay();
            Check(floatState==FloatState.Recognizing,"display text waits for its task terminal event");
            ProcessLine("任务生命周期: 完成 final-a");UpdateDisplay();
            Check(floatState==FloatState.Complete && floatCompleteTick.IsEnabled && floatCompleteTick.Interval.TotalSeconds==1,
                "countdown begins only when the completion check appears");
            PumpDispatcher(200);UpdateDisplay();
            Check(floatState==FloatState.Complete,"check does not disappear immediately");
            PumpDispatcher(950);
            Check(floatState==FloatState.Idle && !floatCompleteTick.IsEnabled && floatWindow.Width==56,
                "real dispatcher timer restores microphone after the default second without restarting on refresh");
            Check(((SolidColorBrush)floatStart.Background).Color.A==0,"microphone restores its transparent icon style");
            ProcessLine("触发：开始录音");ProcessLine("任务生命周期: 录音开始 empty");
            ProcessLine("释放：完成录音");UpdateDisplay();
            ProcessLine("任务生命周期: 录音结束 empty");UpdateDisplay();
            Check(floatState==FloatState.Idle,"empty capture exits recognizing without a false check");
            completionSlider.Value=2.5;floatCompleted=true;UpdateDisplay();
            Check(floatCompleteTick.Interval.TotalSeconds==2.5,"selected duration controls the actual completion timer");
            ProcessLine("触发：开始录音");ProcessLine("任务生命周期: 录音开始 session-new");UpdateDisplay();
            Check(!floatCompleteTick.IsEnabled,"new recording cancels prior completion timeout");
            ProcessLine("任务生命周期: 录音结束 session-old");
            Check(awaitingAudio,"old capture terminal event cannot clear the new capture");
            PumpDispatcher(1100);
            Check(floatState==FloatState.Listening,"old timeout cannot overwrite a new recording");
            ApplyCancelReply("CANCELLED");UpdateDisplay();
            Check(floatState==FloatState.Idle && !processing,"cancellation clears capture handoff waiting");
            ApplyStateReply("STATE|recording=0|connected=1");
            ProcessLine("触发：开始录音");ProcessLine("释放：完成录音");UpdateDisplay();
            Check(floatState==FloatState.Idle && !processing,"older backend cannot leave capture waiting stuck");
            completionSlider.Value=1;
        }
        backend=null;awaitingAudio=false;floatCancelled=false;
    }
    static void RenderPreview(FrameworkElement element,int width,string path) {
        element.Measure(new Size(width,Double.PositiveInfinity));
        double height=element==floatOuter?floatWindow.Height:element.DesiredSize.Height;
        element.Arrange(new Rect(0,0,width,height));element.UpdateLayout();
        var image=new RenderTargetBitmap(width,(int)Math.Ceiling(element.ActualHeight),96,96,PixelFormats.Pbgra32);
        image.Render(element);
        var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(image));
        using(var file=File.Create(path))encoder.Save(file);
    }
}
