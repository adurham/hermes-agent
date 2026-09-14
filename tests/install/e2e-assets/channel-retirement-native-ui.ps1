# Operate the already-running destination's real UI. Never activate/launch it.
param([Parameter(Mandatory=$true)][int]$ProcessId, [Parameter(Mandatory=$true)][string]$Prompt)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
$process = Get-Process -Id $ProcessId -ErrorAction Stop
if ($process.MainWindowHandle -eq 0) { throw 'Destination has no real visible window' }
$root = [System.Windows.Automation.AutomationElement]::RootElement
$condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $ProcessId)
$window = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $condition)
if (-not $window) { throw 'Destination window unavailable to UI Automation' }
$edits = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)))
$editable = @($edits | Where-Object { $_.Current.IsEnabled -and -not $_.Current.IsOffscreen })
if ($editable.Count -ne 1) { throw 'Cannot identify a unique visible chat composer; refusing guessed keystrokes' }
$editable[0].SetFocus()
$pattern = $null
if (-not $editable[0].TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) { throw 'Composer does not expose native text input' }
$pattern.SetValue($Prompt)
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
