#include "SplitNeutrinoPhysics.hh"

// Models and Cross Sections for Neutrino-Electron Scattering
//#include "G4NuElectronChannelProcess.hh"  // <--- Add header
#include "G4HadronicProcess.hh"
#include "G4NeutrinoElectronCcModel.hh"
#include "G4NeutrinoElectronCcXsc.hh"
#include "G4NeutrinoElectronNcModel.hh"
#include "G4NeutrinoElectronNcXsc.hh"
#include "G4NuTauNucleusCcModel.hh"
#include "G4NuTauNucleusNcModel.hh"
#include "G4ANuTauNucleusCcModel.hh"
#include "G4ANuTauNucleusNcModel.hh"
#include "G4NuMuNucleusCcModel.hh"
#include "G4NuMuNucleusNcModel.hh"
#include "G4ANuMuNucleusCcModel.hh"
#include "G4ANuMuNucleusNcModel.hh"
#include "G4NuElNucleusCcModel.hh"
#include "G4NuElNucleusNcModel.hh"
#include "G4ANuElNucleusCcModel.hh"
#include "G4ANuElNucleusNcModel.hh"
#include "G4TauNeutrinoNucleusTotXsc.hh"
#include "G4MuNeutrinoNucleusTotXsc.hh"
#include "G4ElNeutrinoNucleusTotXsc.hh"


// Geant4 Process Management
#include "G4ParticleTable.hh"
#include "G4ProcessManager.hh"

SplitNeutrinoPhysics::SplitNeutrinoPhysics(const G4String& name)
  : G4VPhysicsConstructor(name)
{}

void SplitNeutrinoPhysics::ConstructProcess()
{
    ConstructNuElectronProcesses();

    ConstructNuNucleusProcesses();
}

/* Some interesting things here. We have to use G4HadronicProcess here rather
than G4NeutrinoElectronProcess because G4NeutrinoElectronProcess does not grant
visibilty to the individual CC and NC channels for biasing. However,
G4HadronicProcess, unsurprisingly, expects hadrons. When a neutrino interacts
with an electron, that electron belongs to some hadronic nucleus, so GEANT4 has
already saved what that hadron is. So, G4HadronicProcess includes that nucleus
in its manifest of input particles, but our neutrino electron scattering does
not produce any hadrons in its output. This confuses G4HadronicProcess as it
notices an energy deficit in the output equal to the energy of that original
hadron. G4NeutrinoElectronProcess, which is a subclass of G4HadronicProcess,
skips this check by simply not calling CheckResult() like G4HadronicProcess
does, so we need to make sure that we are similarly allowing ourselves to skip/
ignore this spectator hadron's energy in our output.*/

// Neutrino-Electron Scattering 

void SplitNeutrinoPhysics::ConstructNuElectronProcesses()
{
    // CC Channel
    auto procCC = new G4HadronicProcess("nuElectronCC", fNuElectron);
    //SEE IF YOU CAN REGISTER THIS AS G4HADRONICELASTICPROCESS INSTEAD
    auto modelCC = new MyNuElectronCcModel;
    procCC->RegisterMe(modelCC);
    procCC->AddDataSet(new MyNeutrinoElectronCcXsc());

    RegisterProcessesForNeutrinos(
        {"nu_e", "anti_nu_e", "nu_mu", "nu_tau"},
        {procCC}, 
        {"nuElectron"});

    // NC Channel
    auto procNC = new G4HadronicProcess("nuElectronNC", fNuElectron);
    auto modelNC = new MyNuElectronNcModel;
    procNC->RegisterMe(modelNC);
    procNC->AddDataSet(new MyNeutrinoElectronNcXsc());

    // Register both channels and remove the default combined process "nuElectron"
    RegisterProcessesForNeutrinos(
        {"nu_e", "anti_nu_e", "nu_mu", "anti_nu_mu", "nu_tau", "anti_nu_tau"},
        {procNC}, 
        {"nuElectron"});
}


// Neutrino-Nucleus Scattering Sector

void SplitNeutrinoPhysics::ConstructNuNucleusProcesses()
{
    // Local helper to instantly build and configure a process, saves us a lot
    // of redundancy in creating the processes
    auto createProc = [](const G4String& name, G4HadronicInteraction* model, G4VCrossSectionDataSet* xsc) {
        auto proc = new G4HadronicProcess(name);
        proc->RegisterMe(model);
        proc->AddDataSet(xsc);
        return proc;
    };

    // --- Tau Neutrinos ---
    RegisterProcessesForNeutrinos({"nu_tau"}, {
        createProc("tauNuNucleusCC", new G4NuTauNucleusCcModel(), new MyTauNeutrinoNucleusCcXsc()),
        createProc("tauNuNucleusNC", new G4NuTauNucleusNcModel(), new MyTauNeutrinoNucleusNcXsc())
    }, {"tauNuNucleus"});

    // --- Tau Anti Neutrinos ---
    RegisterProcessesForNeutrinos({"anti_nu_tau"}, {
        createProc("aTauNuNucleusCC", new G4ANuTauNucleusCcModel(), new MyTauNeutrinoNucleusCcXsc()),
        createProc("aTauNuNucleusNC", new G4ANuTauNucleusNcModel(), new MyTauNeutrinoNucleusNcXsc())
    }, {"tauNuNucleus"});

    // --- Muon Neutrinos ---
    RegisterProcessesForNeutrinos({"nu_mu"}, {
        createProc("muNuNucleusCC", new G4NuMuNucleusCcModel(), new MyMuNeutrinoNucleusCcXsc()),
        createProc("muNuNucleusNC", new G4NuMuNucleusNcModel(), new MyMuNeutrinoNucleusNcXsc())
    }, {"muNuNucleus"});

    // --- Muon Anti Neutrinos ---
    RegisterProcessesForNeutrinos({"anti_nu_mu"}, {
        createProc("aMuNuNucleusCC", new G4ANuMuNucleusCcModel(), new MyMuNeutrinoNucleusCcXsc()),
        createProc("aMuNuNucleusNC", new G4ANuMuNucleusNcModel(), new MyMuNeutrinoNucleusNcXsc())
    }, {"muNuNucleus"});

    // --- Electron Neutrinos ---
    RegisterProcessesForNeutrinos({"nu_e"}, {
        createProc("elNuNucleusCC", new G4NuElNucleusCcModel(), new MyElNeutrinoNucleusCcXsc()),
        createProc("elNuNucleusNC", new G4NuElNucleusNcModel(), new MyElNeutrinoNucleusNcXsc())
    }, {"elNuNucleus"});

    // --- Electron Anti Neutrinos ---
    RegisterProcessesForNeutrinos({"anti_nu_e"}, {
        createProc("aElNuNucleusCC", new G4ANuElNucleusCcModel(), new MyElNeutrinoNucleusCcXsc()),
        createProc("aElNuNucleusNC", new G4ANuElNucleusNcModel(), new MyElNeutrinoNucleusNcXsc())
    }, {"elNuNucleus"});

    /*
    // --- Tau Neutrinos: CC and NC ---
    auto tauCC = new G4HadronicProcess("tauNuNucleusCC");
    tauCC->RegisterMe(new G4NuTauNucleusCcModel());
    tauCC->AddDataSet(new MyTauNeutrinoNucleusCcXsc());

    auto tauNC = new G4HadronicProcess("tauNuNucleusNC");
    tauNC->RegisterMe(new G4NuTauNucleusNcModel());
    tauNC->AddDataSet(new MyTauNeutrinoNucleusNcXsc());

    RegisterProcessesForNeutrinos(
        {"nu_tau"},
        {tauCC, tauNC}, 
        {"tauNuNucleus"}
    );

    // --- Tau Anti Neutrinos: CC and NC ---
    auto aTauCC = new G4HadronicProcess("aTauNuNucleusCC");
    aTauCC->RegisterMe(new G4ANuTauNucleusCcModel());
    aTauCC->AddDataSet(new MyTauNeutrinoNucleusCcXsc());

    auto aTauNC = new G4HadronicProcess("aTauNuNucleusNC");
    aTauNC->RegisterMe(new G4ANuTauNucleusNcModel());
    aTauNC->AddDataSet(new MyTauNeutrinoNucleusNcXsc());

    RegisterProcessesForNeutrinos(
        {"anti_nu_tau"}, 
        {aTauCC, aTauNC}, 
        {"tauNuNucleus"}
    );

    // --- Muon Neutrinos: CC and NC ---
    auto muCC = new G4HadronicProcess("muNuNucleusCC");
    muCC->RegisterMe(new G4NuMuNucleusCcModel());
    muCC->AddDataSet(new MyMuNeutrinoNucleusCcXsc());
    //muCC->SetEnergyMomentumCheckLevels(0.001, 10.0 * CLHEP::MeV);
    //muCC->SetEpReportLevel(3);

    auto muNC = new G4HadronicProcess("muNuNucleusNC");
    muNC->RegisterMe(new G4NuMuNucleusNcModel());
    muNC->AddDataSet(new MyMuNeutrinoNucleusNcXsc());

    RegisterProcessesForNeutrinos(
        {"nu_mu"},
        {muCC, muNC}, 
        {"muNuNucleus"}
    );

    // --- Muon Anti Neutrinos: CC and NC ---
    auto aMuCC = new G4HadronicProcess("aMuNuNucleusCC");
    aMuCC->RegisterMe(new G4ANuMuNucleusCcModel());
    aMuCC->AddDataSet(new MyMuNeutrinoNucleusCcXsc());

    auto aMuNC = new G4HadronicProcess("aMuNuNucleusNC");
    aMuNC->RegisterMe(new G4ANuMuNucleusNcModel());
    aMuNC->AddDataSet(new MyMuNeutrinoNucleusNcXsc());

    RegisterProcessesForNeutrinos(
        {"anti_nu_mu"}, 
        {aMuCC, aMuNC}, 
        {"muNuNucleus"}
    );

    // --- Electron Neutrinos: CC and NC ---
    auto elCC = new G4HadronicProcess("elNuNucleusCC");
    elCC->RegisterMe(new G4NuElNucleusCcModel());
    elCC->AddDataSet(new MyElNeutrinoNucleusCcXsc());

    auto elNC = new G4HadronicProcess("elNuNucleusNC");
    elNC->RegisterMe(new G4NuElNucleusNcModel());
    elNC->AddDataSet(new MyElNeutrinoNucleusNcXsc());

    RegisterProcessesForNeutrinos(
        {"nu_e"}, 
        {elCC, elNC}, 
        {"elNuNucleus"}
    );

    // --- Electron Anti Neutrinos: CC and NC ---
    auto aElCC = new G4HadronicProcess("aElNuNucleusCC");
    aElCC->RegisterMe(new G4ANuElNucleusCcModel());
    aElCC->AddDataSet(new MyElNeutrinoNucleusCcXsc());

    auto aElNC = new G4HadronicProcess("aElNuNucleusNC");
    aElNC->RegisterMe(new G4ANuElNucleusNcModel());
    aElNC->AddDataSet(new MyElNeutrinoNucleusNcXsc());

    RegisterProcessesForNeutrinos(
        {"anti_nu_e"}, 
        {aElCC, aElNC}, 
        {"elNuNucleus"}
    );*/
}
// ============================================================================
// Reusable Registration Helper
// ============================================================================
void SplitNeutrinoPhysics::RegisterProcessesForNeutrinos(
    const std::vector<G4String>& neutrinos,
    const std::vector<G4VProcess*>& newProcesses,
    const std::vector<G4String>& processesToRemove)
{

    auto particleTable = G4ParticleTable::GetParticleTable();

    for (const auto& particleName : neutrinos) {
        auto particle = particleTable->FindParticle(particleName);
        if (!particle) continue;

        auto pmanager = particle->GetProcessManager();

        // Remove legacy/combined processes if specified
        for (const auto& procName : processesToRemove) {
            G4ProcessVector* pList = pmanager->GetProcessList();
            for (std::size_t i = 0; i < pList->size(); ++i) {
                if ((*pList)[i]->GetProcessName() == procName) {
                    pmanager->RemoveProcess((*pList)[i]);
                    break; // Break since we removed an element and shifted the list
                }
            }
        }

        // Add the new discrete channel processes
        for (auto newProc : newProcesses) {
            pmanager->AddDiscreteProcess(newProc);
        }
        // --- DIAGNOSTIC PRINTOUT ---
        // Dynamically print out the registered processes for whichever particle is passed
        /*G4cout << "\n=== Registered Processes for " << particleName << " ===" << G4endl;
        G4ProcessVector* pList = pmanager->GetProcessList();
        for (std::size_t i = 0; i < pList->size(); ++i) {
            G4cout << "  Process [" << i << "]: " << (*pList)[i]->GetProcessName() << G4endl;
        }
        G4cout << "===========================================\n" << G4endl;*/
    }
}