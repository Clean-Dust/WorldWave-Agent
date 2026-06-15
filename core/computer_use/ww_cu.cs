using System;
using System.Runtime.InteropServices;

namespace WWCU {
    public class Mouse {
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
        [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT lpPoint);
        [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
        [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
    }
    
    public class Keyboard {
        [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
        [DllImport("user32.dll")] public static extern short VkKeyScan(char ch);
        [DllImport("user32.dll")] public static extern uint MapVirtualKey(uint uCode, uint uMapType);
    }
}
