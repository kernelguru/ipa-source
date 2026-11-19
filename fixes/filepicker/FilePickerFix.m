// FilePickerFix – injected into sideloaded apps by ipa-source.
//
// Sideloaded apps often fail to read files chosen in the system file picker ("no permission"):
// the security-scoped access is issued for an identity that does not match the re-signed app.
// This library makes the picker hand over a *copy* inside the app's own sandbox instead,
// which needs no special access. Folder pickers are left untouched (folders cannot be copied).
//
// Hooks
//   -[UIDocumentPickerViewController initForOpeningContentTypes:asCopy:]  -> asCopy:YES
//   -[UIDocumentPickerViewController initForOpeningContentTypes:]         -> asCopy:YES
//   -[UIDocumentPickerViewController initWithDocumentTypes:inMode:]       -> Open -> Import
//   -[NSURL startAccessingSecurityScopedResource]  -> YES for readable files in our sandbox
//                                                     (copied files are not security-scoped,
//                                                      some apps refuse to continue on NO)

#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#import <objc/runtime.h>
#import <unistd.h>

static BOOL FPFContainsFolder(NSArray *types) {
    for (id t in types) {
        if (@available(iOS 14.0, *)) {
            if ([t isKindOfClass:[UTType class]] && [(UTType *)t conformsToType:UTTypeFolder]) return YES;
        }
        if ([t isKindOfClass:[NSString class]] &&
            ([t isEqualToString:@"public.folder"] || [t isEqualToString:@"public.directory"])) return YES;
    }
    return NO;
}

static void FPFSwizzle(Class cls, SEL sel, IMP newImp, IMP *orig) {
    Method m = class_getInstanceMethod(cls, sel);
    if (!m) return;
    *orig = method_setImplementation(m, newImp);
}

// --- iOS 14+ ---------------------------------------------------------------
static IMP orig_initTypesAsCopy;
static id fpf_initTypesAsCopy(id self, SEL _cmd, NSArray *types, BOOL asCopy) {
    BOOL copy = FPFContainsFolder(types) ? asCopy : YES;
    return ((id (*)(id, SEL, NSArray *, BOOL))orig_initTypesAsCopy)(self, _cmd, types, copy);
}

static IMP orig_initTypes;
static id fpf_initTypes(id self, SEL _cmd, NSArray *types) {
    if (!FPFContainsFolder(types) && orig_initTypesAsCopy) {
        SEL s = @selector(initForOpeningContentTypes:asCopy:);
        return ((id (*)(id, SEL, NSArray *, BOOL))orig_initTypesAsCopy)(self, s, types, YES);
    }
    return ((id (*)(id, SEL, NSArray *))orig_initTypes)(self, _cmd, types);
}

// --- legacy API (deprecated, still used by many apps/frameworks) -----------
static IMP orig_initDocTypesMode;
static id fpf_initDocTypesMode(id self, SEL _cmd, NSArray *types, UIDocumentPickerMode mode) {
    if (mode == UIDocumentPickerModeOpen && !FPFContainsFolder(types)) mode = UIDocumentPickerModeImport;
    return ((id (*)(id, SEL, NSArray *, UIDocumentPickerMode))orig_initDocTypesMode)(self, _cmd, types, mode);
}

// --- security-scoped access -------------------------------------------------
static IMP orig_startAccessing;
static BOOL fpf_startAccessing(NSURL *self, SEL _cmd) {
    BOOL ok = ((BOOL (*)(id, SEL))orig_startAccessing)(self, _cmd);
    if (ok || !self.isFileURL) return ok;
    NSString *path = self.path.stringByStandardizingPath;
    NSString *home = NSHomeDirectory().stringByStandardizingPath;
    NSString *tmp = NSTemporaryDirectory().stringByStandardizingPath;
    BOOL inSandbox = [path hasPrefix:home] || [path hasPrefix:tmp] || [path containsString:@"-Inbox/"] ||
                     [path containsString:@"/tmp/"];
    return inSandbox && access(path.fileSystemRepresentation, R_OK) == 0;
}

__attribute__((constructor)) static void FPFInit(void) {
    @autoreleasepool {
        Class picker = objc_getClass("UIDocumentPickerViewController");
        if (picker) {
            FPFSwizzle(picker, @selector(initForOpeningContentTypes:asCopy:), (IMP)fpf_initTypesAsCopy, &orig_initTypesAsCopy);
            FPFSwizzle(picker, @selector(initForOpeningContentTypes:), (IMP)fpf_initTypes, &orig_initTypes);
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
            FPFSwizzle(picker, @selector(initWithDocumentTypes:inMode:), (IMP)fpf_initDocTypesMode, &orig_initDocTypesMode);
#pragma clang diagnostic pop
        }
        FPFSwizzle([NSURL class], @selector(startAccessingSecurityScopedResource), (IMP)fpf_startAccessing, &orig_startAccessing);
        NSLog(@"[FilePickerFix] active");
    }
}
