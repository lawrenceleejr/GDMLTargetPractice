#ifndef MyExceptionHandler_hh
#define MyExceptionHandler_hh

#include "G4VExceptionHandler.hh"
#include "G4StateManager.hh"
#include "G4RunManager.hh"
#include "G4String.hh"
#include "G4ExceptionSeverity.hh"
#include "G4ios.hh"

/* Custom exception handler that is used to catch the offshell energy problems
that arise from high-energy neutrino interactions. This exception handler
silence the warnings that are typically printed to the terminal when resampling
after detecting an off shell dynamic mass. Typically, if GEANT4 fails to sample
an on shell dynamic mass after 100 attempts, it crashes. This exception handler
intercepts that error, just aborts that event, and continues with the run.*/
class MyExceptionHandler : public G4VExceptionHandler {
public:
    MyExceptionHandler() {
        // Register this handler with Geant4's state manager for the current thread
        G4StateManager::GetStateManager()->SetExceptionHandler(this);
    }

    ~MyExceptionHandler() override = default;

    G4bool Notify(const char* originOfException,
                  const char* exceptionCode,
                  G4ExceptionSeverity severity,
                  const char* description) override
    {
        G4String code(exceptionCode);

        // Mute had012 warnings entirely (dynamic mass off-shell resampling notices)
        if (code == "had012") {
            return false; // false tells Geant4 "exception handled, do not terminate"
        }

        // Intercept unrecoverable had006 errors from off shell mass problems
        if (code == "had006") {
            G4cout << "\n>>> [MyExceptionHandler] Intercepted unrecoverable exception (" 
                   << code << ") in " << originOfException 
                   << ". Aborting event and continuing run." << G4endl;

            auto runManager = G4RunManager::GetRunManager();
            if (runManager) {
                runManager->AbortEvent();
            }

            return false; // Returning false PREVENTS Geant4 from executing standard std::abort()
        }

        // Allow any other standard warnings to print normally
        return false;
    }
};

#endif