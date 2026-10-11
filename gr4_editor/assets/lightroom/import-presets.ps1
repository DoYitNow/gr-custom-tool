param([Parameter(Mandatory=$true)][string]$ProfilePath,[Parameter(Mandatory=$true)][string]$PresetPath)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding
$ProfilePath=(Get-Item -LiteralPath $ProfilePath).FullName
$PresetPath=(Get-Item -LiteralPath $PresetPath).FullName
$folder=[IO.Path]::GetDirectoryName($ProfilePath)
if($folder -ne [IO.Path]::GetDirectoryName($PresetPath)){throw '自动导入的配置文件和预设须位于同一目录'}
$classic=Get-Process -Name Lightroom -ErrorAction SilentlyContinue | Select-Object -First 1
if(!$classic){exit 0}
Add-Type -TypeDefinition @'
using System;using System.Text;using System.Runtime.InteropServices;
public static class GR4Import {
 public delegate bool EnumWindow(IntPtr h,IntPtr p);
 [DllImport("user32.dll")]public static extern bool EnumWindows(EnumWindow callback,IntPtr p);
 [DllImport("user32.dll")]public static extern bool EnumChildWindows(IntPtr h,EnumWindow callback,IntPtr p);
 [DllImport("user32.dll")]public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("user32.dll")]public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern int GetClassName(IntPtr h,StringBuilder s,int n);
 [DllImport("user32.dll")]public static extern IntPtr GetParent(IntPtr h);
 [DllImport("user32.dll")]public static extern IntPtr GetMenu(IntPtr h);
 [DllImport("user32.dll")]public static extern IntPtr GetSubMenu(IntPtr h,int i);
 [DllImport("user32.dll")]public static extern int GetMenuItemCount(IntPtr h);
 [DllImport("user32.dll")]public static extern uint GetMenuItemID(IntPtr h,int i);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern int GetMenuString(IntPtr h,uint i,StringBuilder s,int n,uint f);
 [DllImport("user32.dll")]public static extern bool PostMessage(IntPtr h,uint m,IntPtr w,IntPtr l);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,string l);
 [DllImport("user32.dll")]public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,IntPtr l);
}
'@
function Window-Text([IntPtr]$handle){$buffer=New-Object System.Text.StringBuilder 600;[GR4Import]::GetWindowText($handle,$buffer,600)|Out-Null;return $buffer.ToString()}
function Window-Class([IntPtr]$handle){$buffer=New-Object System.Text.StringBuilder 100;[GR4Import]::GetClassName($handle,$buffer,100)|Out-Null;return $buffer.ToString()}
function Classic-Windows {
 $windows=New-Object 'System.Collections.Generic.List[System.IntPtr]'
 [GR4Import]::EnumWindows({param($handle,$parameter);$owner=[uint32]0;[GR4Import]::GetWindowThreadProcessId($handle,[ref]$owner)|Out-Null;if($owner -eq $classic.Id -and [GR4Import]::IsWindowVisible($handle)){$windows.Add($handle)};return $true},[IntPtr]::Zero)|Out-Null
 return $windows.ToArray()
}
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Accept-DuplicateNotices([IntPtr]$except=[IntPtr]::Zero){
 foreach($notice in @(Classic-Windows | Where-Object {$_ -ne $except -and (Window-Class $_) -eq '#32770'})){
  $element=[System.Windows.Automation.AutomationElement]::FromHandle($notice)
  $names=@($element.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)|ForEach-Object{$_.Current.Name})
  if(($names -join ' ') -notmatch '所有项目已导入|All (items|profiles and presets) (?:(?:have|are|were) )?already (?:been )?imported'){
   throw ('Classic 有待处理的导入提示或对话框：'+($names -join ' '))
  }
  $noticeChildren=New-Object 'System.Collections.Generic.List[System.IntPtr]'
  [GR4Import]::EnumChildWindows($notice,{param($handle,$parameter);$noticeChildren.Add($handle);return $true},[IntPtr]::Zero)|Out-Null
  $ack=$noticeChildren|Where-Object{(Window-Class $_) -eq 'Button' -and (Window-Text $_) -match '^(&?确定|&?OK)$'}|Select-Object -First 1
  if(!$ack){throw 'Classic 重复导入提示没有可识别的确定按钮'}
  [GR4Import]::SendMessage($ack,0xF5,[IntPtr]::Zero,[IntPtr]::Zero)|Out-Null
  Write-Output 'Existing profile/preset acknowledged; SDK and JPEG audits must still pass.'
 }
}
function Import-Command([IntPtr]$menu){
 for($index=0;$index -lt [GR4Import]::GetMenuItemCount($menu);$index++){
  $buffer=New-Object System.Text.StringBuilder 300;[GR4Import]::GetMenuString($menu,$index,$buffer,300,0x400)|Out-Null
  if($buffer.ToString() -match '导入修改照片配置文件和预设|Import Develop Profiles and Presets'){return [GR4Import]::GetMenuItemID($menu,$index)}
  $child=[GR4Import]::GetSubMenu($menu,$index)
  if($child -ne [IntPtr]::Zero){$command=Import-Command $child;if($command){return $command}}
 }
}
$main=Classic-Windows | Where-Object {(Window-Text $_) -match 'Lightroom Classic' -and [GR4Import]::GetMenu($_) -ne [IntPtr]::Zero} | Select-Object -First 1
if(!$main){throw 'Lightroom Classic 尚未进入可操作的目录界面'}
$command=Import-Command ([GR4Import]::GetMenu($main))
if(!$command){throw '当前 Classic 菜单没有 Import Develop Profiles and Presets；适配支持英文或中文界面'}
Accept-DuplicateNotices
[GR4Import]::PostMessage($main,0x111,[IntPtr]$command,[IntPtr]::Zero)|Out-Null
$dialog=$null;$deadline=[DateTime]::UtcNow.AddSeconds(15)
while(!$dialog -and [DateTime]::UtcNow -lt $deadline){
 $dialog=Classic-Windows | Where-Object {(Window-Class $_) -eq '#32770' -and (Window-Text $_) -match '导入修改照片配置文件和预设|Import Develop Profiles and Presets'} | Select-Object -First 1
 if(!$dialog){Start-Sleep -Milliseconds 200}
}
if(!$dialog){throw 'Classic 没有打开预设导入文件对话框'}
$children=New-Object 'System.Collections.Generic.List[System.IntPtr]'
[GR4Import]::EnumChildWindows($dialog,{param($handle,$parameter);$children.Add($handle);return $true},[IntPtr]::Zero)|Out-Null
$edit=$children | Where-Object {(Window-Class $_) -eq 'Edit' -and (Window-Class ([GR4Import]::GetParent($_))) -eq 'ComboBox'} | Select-Object -First 1
$button=$children | Where-Object {(Window-Class $_) -eq 'Button' -and (Window-Text $_) -match '^(&?导入|&?Import|&?打开|&?Open)'} | Select-Object -First 1
if(!$edit -or !$button){throw 'Classic 的标准导入对话框没有预期的文件名/导入控件'}
# Navigate first, then select names within that directory. This keeps paths out
# of the file-name list and supports spaces and Chinese names in the folder.
[GR4Import]::SendMessage($edit,0xC,[IntPtr]::Zero,($folder+'\'))|Out-Null
[GR4Import]::SendMessage($button,0xF5,[IntPtr]::Zero,[IntPtr]::Zero)|Out-Null
$located=$false;$deadline=[DateTime]::UtcNow.AddSeconds(5)
while(!$located -and [DateTime]::UtcNow -lt $deadline){
 $locationControls=New-Object 'System.Collections.Generic.List[System.IntPtr]'
 [GR4Import]::EnumChildWindows($dialog,{param($handle,$parameter);$locationControls.Add($handle);return $true},[IntPtr]::Zero)|Out-Null
 $located=@($locationControls | Where-Object {(Window-Class $_) -eq 'ToolbarWindow32' -and (Window-Text $_).EndsWith($folder,[StringComparison]::OrdinalIgnoreCase)}).Count -gt 0
 if(!$located){Start-Sleep -Milliseconds 100}
}
if(!$located){throw 'Classic 导入框未进入预设文件目录'}
$files='"'+[IO.Path]::GetFileName($ProfilePath)+'" "'+[IO.Path]::GetFileName($PresetPath)+'"'
[GR4Import]::SendMessage($edit,0xC,[IntPtr]::Zero,$files)|Out-Null
[GR4Import]::SendMessage($button,0xF5,[IntPtr]::Zero,[IntPtr]::Zero)|Out-Null
$deadline=[DateTime]::UtcNow.AddSeconds(10)
while([GR4Import]::IsWindowVisible($dialog) -and [DateTime]::UtcNow -lt $deadline){Accept-DuplicateNotices $dialog;Start-Sleep -Milliseconds 200}
if([GR4Import]::IsWindowVisible($dialog)){throw '预设导入对话框尚未关闭，请检查 Classic 提示'}
# Classic can post its duplicate warning after the file dialog has closed.
# Poll through that delay instead of testing just once after 300 ms.
$deadline=[DateTime]::UtcNow.AddSeconds(3)
while([DateTime]::UtcNow -lt $deadline){Accept-DuplicateNotices;Start-Sleep -Milliseconds 100}
Accept-DuplicateNotices
Write-Output 'Native profile/preset import requested; SDK and JPEG audits must still pass.'
