#include "BiasingMessenger.hh"
#include "Biasing.hh"
#include "G4UIdirectory.hh"
#include "G4UIcmdWithADouble.hh"
#include <vector>

// struct to hold bias command data
struct BiasConfig {
    G4String cmdName;
    G4String guidance;
    G4String processKey;
};

BiasingMessenger::BiasingMessenger(Biasing* biasing)
  : G4UImessenger(), fBiasing(biasing)
{
    // Create the directory for your custom commands
    fBiasingDir = new G4UIdirectory("/custom/biasing/");
    fBiasingDir->SetGuidance("Custom UI commands for neutrino biasing.");

    // Define all biasing information in a vector
    std::vector<BiasConfig> configs = {
        {"NuElectronCcBias", "Set the bias factor for neutrino-electron CC interactions (nuElectronCC).", "nuElectronCC"},
        {"NuElectronNcBias", "Set the bias factor for neutrino-electron NC interactions (nuElectronNC).", "nuElectronNC"},
        {"TauNuNucleusCcBias", "Set the bias factor for tau neutrino-nucleus processes (tauNuNucleusCC).", "tauNuNucleusCC"},
        {"TauNuNucleusNcBias", "Set the bias factor for tau neutrino-nucleus processes (tauNuNucleusNC).", "tauNuNucleusNC"},
        {"TauANuNucleusCcBias", "Set the bias factor for tau anti neutrino-nucleus processes (tauANuNucleusCC).", "aTauNuNucleusCC"},
        {"TauANuNucleusNcBias", "Set the bias factor for tau anti neutrino-nucleus processes (tauANuNucleusNC).", "aTauNuNucleusNC"},
        {"MuNuNucleusCcBias", "Set the bias factor for muon neutrino-nucleus processes (muNuNucleusCC).", "muNuNucleusCC"},
        {"MuNuNucleusNcBias", "Set the bias factor for muon neutrino-nucleus processes (muNuNucleusNC).", "muNuNucleusNC"},
        {"MuANuNucleusCcBias", "Set the bias factor for muon anti neutrino-nucleus processes (muANuNucleusCC).", "aMuNuNucleusCC"},
        {"MuANuNucleusNcBias", "Set the bias factor for muon anti neutrino-nucleus processes (muANuNucleusNC).", "aMuNuNucleusNC"},
        {"ElNuNucleusCcBias", "Set the bias factor for electron neutrino-nucleus processes (elNuNucleusCC).", "elNuNucleusCC"},
        {"ElNuNucleusNcBias", "Set the bias factor for electron neutrino-nucleus processes (elNuNucleusNC).", "elNuNucleusNC"},
        {"ElANuNucleusCcBias", "Set the bias factor for electron anti neutrino-nucleus processes (elANuNucleusCC).", "aElNuNucleusCC"},
        {"ElANuNucleusNcBias", "Set the bias factor for electron anti neutrino-nucleus processes (elANuNucleusNC).", "aElNuNucleusNC"}
    };

    // loop through the vector and populate the commands
    for (const auto& config : configs) {
        G4String cmdPath = "/custom/biasing/" + config.cmdName;
        G4UIcmdWithADouble* cmd = new G4UIcmdWithADouble(cmdPath, this);
        cmd->SetGuidance(config.guidance);
        cmd->SetParameterName("factor", false);
        cmd->AvailableForStates(G4State_PreInit, G4State_Idle);

        // Store the command pointer and its associated process key
        fCommandMap[cmd] = config.processKey;
    }

}

// This is nice because it is much smaller and cleaner than typing out each name
BiasingMessenger::~BiasingMessenger()
{
    // Clean up all dynamically allocated commands
    for (auto const& [cmd, key] : fCommandMap) {
        delete cmd;
    }
    delete fBiasingDir;
}

// Just a setter for each biasing factor
void BiasingMessenger::SetNewValue(G4UIcommand* command, G4String newValue)
{
    // cast generic command pointer to a specific double command pointer
    G4UIcmdWithADouble* doubleCmd = static_cast<G4UIcmdWithADouble*>(command);
    
    // Look up the command in the map to find its corresponding process key
    auto it = fCommandMap.find(doubleCmd);
    if (it != fCommandMap.end()) {
        fBiasing->SetBiasFactor(it->second, doubleCmd->GetNewDoubleValue(newValue));
    }
}