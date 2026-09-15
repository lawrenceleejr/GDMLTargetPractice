#include "RunAction.hh"
#include "RunActionMessenger.hh"
#include "EventAction.hh"
#include "PrimaryGenerator.hh"
#include "G4Run.hh"
#include "G4Threading.hh"
#include <cstdlib>

#ifdef USE_CELERITAS
#include <CeleritasG4.hh>
#endif

namespace {
bool IsNeutrino(int pdg) {
    int a = std::abs(pdg);
    return a == 12 || a == 14 || a == 16;
}
}  // namespace

RunAction::RunAction()
: G4UserRunAction()
{
    auto analysisManager = G4AnalysisManager::Instance();
    fMessenger = new RunActionMessenger(this);
}

RunAction::~RunAction() { delete fMessenger; }

void RunAction::BeginOfRunAction(const G4Run* run) {
#ifdef USE_CELERITAS
    celeritas::TrackingManagerIntegration::Instance().BeginOfRunAction(run);
#endif

    // Resolve whether to emit the neutrino-interaction branches.
    fNeutrinoBranches = (fNuMode == NuMode::kOn);
    if (fNuMode == NuMode::kAuto && fGenerator) {
        fNeutrinoBranches = IsNeutrino(fGenerator->GetParticlePDG());
    }
    G4cout << "Output ntuple: neutrino-interaction branches "
           << (fNeutrinoBranches ? "ENABLED" : "disabled")
           << " (/analysis/neutrinoMode)." << G4endl;

    // JTR: Adding filenaming from a macro if desired, helps avoid overwrites
    fExitWriter.BeginRun();

    // Robust check: True for MT-Workers, AND true for Sequential mode
    bool isEventProcessor = true; 
    if (G4Threading::IsMultithreadedApplication()) {
        isEventProcessor = !IsMaster();
    }

    // Only allow the event-processing thread to open the ROOT file.
    if (isEventProcessor) {
        G4String baseName = G4AnalysisManager::Instance()->GetFileName();
        if (baseName.empty()) {
            baseName = "output";
        } 
        size_t rootExt = baseName.find(".root");
        if (rootExt != std::string::npos) {
            baseName = baseName.substr(0, rootExt);
        }

        // Inject the Run ID
        G4String fileName = baseName + "_" + std::to_string(run->GetRunID()) + ".root";

        fFile = new TFile(fileName.c_str(), "RECREATE");
        fTree = new TTree("tree", "Simulation data");

        // --- Event scalars ---
        fTree->Branch("eventID", &eventID, "eventID/I");
        fTree->Branch("primaryPDG", &primaryPDG, "primaryPDG/I");
        fTree->Branch("primaryE", &primaryE, "primaryE/D");
        fTree->Branch("primaryStartX", &primaryStartX, "primaryStartX/D");
        fTree->Branch("primaryStartY", &primaryStartY, "primaryStartY/D");
        fTree->Branch("primaryStartZ", &primaryStartZ, "primaryStartZ/D");
        fTree->Branch("primaryStartPx", &primaryStartPx, "primaryStartPx/D");
        fTree->Branch("primaryStartPy", &primaryStartPy, "primaryStartPy/D");
        fTree->Branch("primaryStartPz", &primaryStartPz, "primaryStartPz/D");
        fTree->Branch("primaryEndE", &primaryEndE, "primaryEndE/D");
        fTree->Branch("primaryEndX", &primaryEndX, "primaryEndX/D");
        fTree->Branch("primaryEndY", &primaryEndY, "primaryEndY/D");
        fTree->Branch("primaryEndZ", &primaryEndZ, "primaryEndZ/D");
        fTree->Branch("primaryEndPx", &primaryEndPx, "primaryEndPx/D");
        fTree->Branch("primaryEndPy", &primaryEndPy, "primaryEndPy/D");
        fTree->Branch("primaryEndPz", &primaryEndPz, "primaryEndPz/D");
        fTree->Branch("totalEdep", &totalEdep, "totalEdep/D");
        fTree->Branch("nSteps", &nSteps, "nSteps/I");
        fTree->Branch("nTracks", &nTracks, "nTracks/I");

        // --- Per-track vectors ---
        fTree->Branch("trk_id", &trk_id);
        fTree->Branch("trk_parentID", &trk_parentID);
        fTree->Branch("trk_pdg", &trk_pdg);
        fTree->Branch("trk_startX", &trk_startX);
        fTree->Branch("trk_startY", &trk_startY);
        fTree->Branch("trk_startZ", &trk_startZ);
        fTree->Branch("trk_startE", &trk_startE);
        fTree->Branch("trk_endX", &trk_endX);
        fTree->Branch("trk_endY", &trk_endY);
        fTree->Branch("trk_endZ", &trk_endZ);
        fTree->Branch("trk_endE", &trk_endE);
        fTree->Branch("trk_edep", &trk_edep);
        fTree->Branch("trk_length", &trk_length);
        fTree->Branch("trk_creatorProcess", &trk_creatorProcess);

        // --- Per-step vectors ---
        fTree->Branch("step_trackID", &step_trackID);
        fTree->Branch("step_pdg", &step_pdg);
        fTree->Branch("step_x", &step_x);
        fTree->Branch("step_y", &step_y);
        fTree->Branch("step_z", &step_z);
        fTree->Branch("step_kinE", &step_kinE);
        fTree->Branch("step_edep", &step_edep);
        fTree->Branch("step_length", &step_length);
        fTree->Branch("step_time", &step_time);
        fTree->Branch("step_process", &step_process);

        // --- Neutrino-interaction block (only when enabled) ---
        if (fNeutrinoBranches) {
            fTree->Branch("nu_isCC", &nu_isCC, "nu_isCC/O");
            fTree->Branch("nu_isNC", &nu_isNC, "nu_isNC/O");
            fTree->Branch("nu_interactionProcess", &nu_interactionProcess);
            fTree->Branch("nu_vertexX", &nu_vertexX, "nu_vertexX/D");
            fTree->Branch("nu_vertexY", &nu_vertexY, "nu_vertexY/D");
            fTree->Branch("nu_vertexZ", &nu_vertexZ, "nu_vertexZ/D");
            fTree->Branch("nu_vertexT", &nu_vertexT, "nu_vertexT/D");
            fTree->Branch("nu_targetZ", &nu_targetZ, "nu_targetZ/I");
            fTree->Branch("nu_targetA", &nu_targetA, "nu_targetA/I");
            fTree->Branch("nu_outLeptonPDG", &nu_outLeptonPDG, "nu_outLeptonPDG/I");
            fTree->Branch("nu_outLeptonE", &nu_outLeptonE, "nu_outLeptonE/D");
            fTree->Branch("nu_outLeptonPx", &nu_outLeptonPx, "nu_outLeptonPx/D");
            fTree->Branch("nu_outLeptonPy", &nu_outLeptonPy, "nu_outLeptonPy/D");
            fTree->Branch("nu_outLeptonPz", &nu_outLeptonPz, "nu_outLeptonPz/D");
            fTree->Branch("nu_Q2", &nu_Q2, "nu_Q2/D");
            fTree->Branch("nu_W", &nu_W, "nu_W/D");
            fTree->Branch("nu_x", &nu_x, "nu_x/D");
            fTree->Branch("nu_y", &nu_y, "nu_y/D");
            fTree->Branch("nu_q0", &nu_q0, "nu_q0/D");
        }
    }
}

void RunAction::FillEvent(EventAction* evt)
{
    // --- Per-track table (map iterates ascending by track id) ---
    trk_id.clear(); trk_parentID.clear(); trk_pdg.clear();
    trk_startX.clear(); trk_startY.clear(); trk_startZ.clear(); trk_startE.clear();
    trk_endX.clear(); trk_endY.clear(); trk_endZ.clear(); trk_endE.clear();
    trk_edep.clear(); trk_length.clear(); trk_creatorProcess.clear();

    for (const auto& [id, t] : evt->trackTable) {
        trk_id.push_back(id);
        trk_parentID.push_back(t.parentID);
        trk_pdg.push_back(t.pdg);
        trk_startX.push_back(t.startX);
        trk_startY.push_back(t.startY);
        trk_startZ.push_back(t.startZ);
        trk_startE.push_back(t.startE);
        trk_endX.push_back(t.endX);
        trk_endY.push_back(t.endY);
        trk_endZ.push_back(t.endZ);
        trk_endE.push_back(t.endE);
        trk_edep.push_back(t.edep);
        trk_length.push_back(t.length);
        trk_creatorProcess.push_back(t.creatorProcess);
    }

    // --- Per-step vectors ---
    step_trackID.clear(); step_pdg.clear();
    step_x.clear(); step_y.clear(); step_z.clear();
    step_kinE.clear(); step_edep.clear(); step_length.clear(); step_time.clear();
    step_process.clear();

    for (const auto& s : evt->steps) {
        step_trackID.push_back(s.trackID);
        step_pdg.push_back(s.PDG);
        step_x.push_back(s.prePos.x());
        step_y.push_back(s.prePos.y());
        step_z.push_back(s.prePos.z());
        step_kinE.push_back(s.preKinE);
        step_edep.push_back(s.edep);
        step_length.push_back(s.stepLength);
        step_time.push_back(s.globalTime);
        step_process.push_back(s.processName);
    }
}

void RunAction::EndOfRunAction(const G4Run* run) {
#ifdef USE_CELERITAS
    celeritas::TrackingManagerIntegration::Instance().EndOfRunAction(run);
#endif
    fExitWriter.EndRun();

    bool isEventProcessor = true; 
    if (G4Threading::IsMultithreadedApplication()) {
        isEventProcessor = !IsMaster();
    }

    // MT Protection: Only the event processor interacts with the ROOT file
    if (isEventProcessor) {
        if (fFile) {
            fFile->cd();
            fTree->Write();
            fFile->Close();
            delete fFile;
            fFile = nullptr; // Reset the pointer for the next loop iteration
        }
    }
}
