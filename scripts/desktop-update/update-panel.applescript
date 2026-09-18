# update-panel.applescript — the renderer-free macOS shim.
#
# When no Chromium-family browser may host ui.html (Safari/Firefox default, or
# no Chrome installed), this draws the same progress as a tiny native panel:
# an AppKit window whose label polls the SAME status file serve-ui.py's HTTP
# page reads (write_status JSON: {"status":..,"message":..}). No network, no
# browser, and no scripting of other apps, so no TCC automation prompt —
# AppKit is driven in-process under /usr/bin/osascript.
#
# Lifecycle mirrors the browser shim: poll until a terminal status, then linger
# (stop_ui leave-window sleeps 15s before killing us on manual/error; "done"
# tears down immediately, so exit on our own shortly after). A closed window
# ends the process — the user is allowed to dismiss us.
#
#   /usr/bin/osascript update-panel.applescript <status-file>
#
# Must remain here, next to posix.sh: the desktop spawns this directory as-is
# (see resolvePosixScriptHandoff) and the script references it via $SCRIPT_DIR.

use framework "Foundation"
use framework "AppKit"
use scripting additions

# Set once the status file has been seen to exist; a LATER disappearance
# means the shim published a terminal state and cleaned up (see run handler).
property everPublished : false

on run argv
	if (count of argv) < 1 then error "usage: osascript update-panel.applescript <status-file>"
	set statusFile to (item 1 of argv as text)

	set nsapp to current application's NSApplication's sharedApplication()
	# Accessory: no Dock icon — this is a status panel, not an app.
	nsapp's setActivationPolicy:(current application's NSApplicationActivationPolicyAccessory)

	set win to current application's NSWindow's alloc()'s initWithContentRect:(current application's NSMakeRect(0, 0, 340, 110)) styleMask:3 backing:(current application's NSBackingStoreBuffered) defer:(current application's NSNumber's numberWithBool:true)
	win's setTitle:"Hermes update"
	win's |center|()
	win's setReleasedWhenClosed:(current application's NSNumber's numberWithBool:false)

	set label to current application's NSTextField's wrappingLabelWithString:"Preparing update…"
	label's setFrameSize:(current application's NSMakeSize(316, 50))
	label's setFrameOrigin:(current application's NSMakePoint(12, 46))
	label's setEditable:(current application's NSNumber's numberWithBool:false)
	label's setBordered:(current application's NSNumber's numberWithBool:false)
	label's setDrawsBackground:(current application's NSNumber's numberWithBool:false)
	label's setFont:(current application's NSFont's systemFontOfSize:(13))

	set bar to current application's NSProgressIndicator's alloc()'s initWithFrame:(current application's NSMakeRect(12, 14, 316, 16))
	bar's setIndeterminate:(current application's NSNumber's numberWithBool:true)
	bar's startAnimation:(missing value)

	set content to win's contentView()
	content's addSubview:label
	content's addSubview:bar
	win's makeKeyAndOrderFront:(missing value)
	nsapp's activateIgnoringOtherApps:(true)

	set state to "running"
	set runloop to current application's NSRunLoop's currentRunLoop()
	repeat
		set stPair to my readStatus(statusFile)
		if stPair is not missing value then
			set state to item 1 of stPair
			set message to item 2 of stPair
			if message is not "" then label's setStringValue:message
			if state is in {"done", "manual", "error"} then
				bar's stopAnimation:(missing value)
				exit repeat
			end if
		else if my everPublished then
			# The status file vanished after having existed: the shim removes
			# it right after publishing a terminal state and tearing down the
			# browser/panel UI. Waiting forever here stranded the panel on its
			# last stage ("Installing the new app") — observed on a real
			# update. Treat the disappearance as done; an error publish keeps
			# its file alive through the 15s leave-window grace, so this
			# cannot mask a failure.
			set state to "done"
			bar's stopAnimation:(missing value)
			exit repeat
		else
			set everPublished to true
		end if
		# Pump the main runloop: without this the window never repaints and
		# macOS beachballs the panel (label frozen on its initial string).
		runloop's runMode:(current application's NSDefaultRunLoopMode) beforeDate:(current application's NSDate's dateWithTimeIntervalSinceNow:0.5)
	end repeat
	if state is "done" then
		# The app is coming back; the shim's job is over.
		delay 1.5
	else
		# manual/error: mirror stop_ui leave-window so the message is readable
		# even without it (the durable result dialog covers the rest).
		delay 15
	end if
end run

on readStatus(statusFile)
	# write_status emits a flat, one-line JSON pair we control, so field
	# extraction beats NSJSONSerialization here (whose |error|: label this
	# osascript rejects in by-ref position). Values are json_escaped by the
	# writer; only \" can appear inside them and it carries no delimiter risk
	# for the "..."-capturing pattern below. A missing/unreadable status file
	# just yields no update this tick — the writer's atomic mv guarantees we
	# never see a partial file.
	set raw to ""
	try
		set raw to do shell script "/usr/bin/grep -o '\\\"status\\\":\\\"[^\\\"]*\\\"' " & quoted form of statusFile & " | /usr/bin/head -1"
	end try
	if raw is "" then return missing value
	set AppleScript's text item delimiters to "\""
	set state to text item 4 of raw
	set rawMsg to ""
	try
		set rawMsg to do shell script "/usr/bin/grep -o '\\\"message\\\":\\\"[^\\\"]*\\\"' " & quoted form of statusFile & " | /usr/bin/head -1"
	end try
	set msg to ""
	if rawMsg is not "" then set msg to text item 4 of rawMsg
	set AppleScript's text item delimiters to ""
	return {state, msg}
end readStatus
