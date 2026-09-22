#ifndef RUNACTION_H
#define RUNACTION_H

#include "G4UserRunAction.hh"
#include "G4AnalysisManager.hh"
#include "TFile.h"
#include "TTree.h"
#include "globals.hh"
#include "ExitWriter.hh"
#include <vector>
#include <string>

class EventAction;
class PrimaryGenerator;
class RunActionMessenger;

class RunAction : public G4UserRunAction {
public:
    // Neutrino-interaction output mode. kAuto enables the nu_* branches when
    // the primary particle is a neutrino; kOn/kOff force the choice.
    enum class NuMode { kOff, kOn, kAuto };

    RunAction();
    ~RunAction() override;

    void FillEvent(EventAction* evt);
    void BeginOfRunAction(const G4Run*) override;
    void EndOfRunAction(const G4Run*) override;

    TTree* GetTree() { return fTree; }

    // Configuration (messenger / main)
    void SetNeutrinoMode(NuMode mode) { fNuMode = mode; }
    void SetGenerator(PrimaryGenerator* gen) { fGenerator = gen; }
    /// Optional HepMC3 export of particles leaving a volume (see ExitWriter).
    ExitWriter* GetExitWriter() { return &fExitWriter; }
    bool NeutrinoBranchesEnabled() const { return fNeutrinoBranches; }

    // ============================================================
    // Branch variables (written by EventAction at end of event)
    // ============================================================

    // --- Event scalars ---
    int eventID = 0;
    int primaryPDG = 0;
    int nSteps = 0;
    int nTracks = 0;
    double primaryE = 0;
    double primaryStartX = 0, primaryStartY = 0, primaryStartZ = 0;
    double primaryStartPx = 0, primaryStartPy = 0, primaryStartPz = 0;
    double primaryEndE = 0;
    double primaryEndX = 0, primaryEndY = 0, primaryEndZ = 0;
    double primaryEndPx = 0, primaryEndPy = 0, primaryEndPz = 0;
    double totalEdep = 0;

    ExitWriter fExitWriter;

    // --- Per-track vectors (one entry per track) ---
    std::vector<int> trk_id, trk_parentID, trk_pdg;
    std::vector<double> trk_startX, trk_startY, trk_startZ, trk_startE;
    std::vector<double> trk_endX, trk_endY, trk_endZ, trk_endE;
    std::vector<double> trk_edep, trk_length;
    std::vector<std::string> trk_creatorProcess;

    // --- Per-step vectors (one entry per step) ---
    std::vector<int> step_trackID, step_pdg;
    std::vector<double> step_x, step_y, step_z;
    std::vector<double> step_kinE, step_edep, step_length, step_time;
    std::vector<std::string> step_process;

    // --- Neutrino-interaction block (booked only in neutrino mode) ---
    bool nu_isCC = false, nu_isNC = false;
    std::string nu_interactionProcess;
    double nu_vertexX = 0, nu_vertexY = 0, nu_vertexZ = 0, nu_vertexT = 0;
    int nu_targetZ = -1, nu_targetA = -1;
    int nu_outLeptonPDG = 0;
    double nu_outLeptonE = 0, nu_outLeptonPx = 0, nu_outLeptonPy = 0, nu_outLeptonPz = 0;
    double nu_Q2 = 0, nu_W = 0, nu_x = 0, nu_y = 0, nu_q0 = 0;

private:
    TFile* fFile = nullptr;
    TTree* fTree = nullptr;

    NuMode fNuMode = NuMode::kAuto;
    bool fNeutrinoBranches = false;   // resolved in BeginOfRunAction
    PrimaryGenerator* fGenerator = nullptr;
    RunActionMessenger* fMessenger = nullptr;
};

#endif
