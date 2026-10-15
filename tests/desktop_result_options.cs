using System;
using System.IO;
using System.Windows;
using System.Windows.Controls;

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
    [STAThread] static void Main() { Desktop.CheckResultOptions(); }
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
        Console.WriteLine("PASS: result selection, full text, final-only copy, switches and preference round-trip");
    }
}
