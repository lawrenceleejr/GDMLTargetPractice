#ifndef Biasing_h
#define Biasing_h 1

#include "G4VBiasingOperator.hh"
#include "G4ParticleDefinition.hh"
#include "G4String.hh"

#include <map>
#include <set>
#include <vector>

class G4BOptnChangeCrossSection;
class G4BiasingProcessInterface;
class BiasingMessenger;

// List of the names of the neutrino processes. Makes things cleaner later
namespace BiasingConfig {
    const std::vector<G4String> ProcessNames = {
        "tauNuNucleusCC", "tauNuNucleusNC", "muNuNucleusCC", "muNuNucleusNC",
        "elNuNucleusCC", "elNuNucleusNC", "aTauNuNucleusCC", "aTauNuNucleusNC",
        "aMuNuNucleusCC", "aMuNuNucleusNC", "aElNuNucleusCC", "aElNuNucleusNC",
        "nuElectronCC", "nuElectronNC"
    };
}

class Biasing : public G4VBiasingOperator
{
public:
    void SetBiasFactor(const G4String& processName, G4double factor);
    Biasing(const G4String& name, G4double biasFactor = 1.0);

    virtual ~Biasing();

    virtual void StartRun() override;
    //virtual void StartTracking(const G4Track*) override;

private:

    virtual G4VBiasingOperation*
    ProposeOccurenceBiasingOperation(
        const G4Track*,
        const G4BiasingProcessInterface*) override;

    virtual G4VBiasingOperation*
    ProposeFinalStateBiasingOperation(
        const G4Track*,
        const G4BiasingProcessInterface*) override
    {
        return nullptr;
    }

    virtual G4VBiasingOperation*
    ProposeNonPhysicsBiasingOperation(
        const G4Track*,
        const G4BiasingProcessInterface*) override
    {
        return nullptr;
    }

private:

    using G4VBiasingOperator::OperationApplied;

    virtual void OperationApplied(
        const G4BiasingProcessInterface*,
        G4BiasingAppliedCase,
        G4VBiasingOperation*,
        G4double,
        G4VBiasingOperation*,
        const G4VParticleChange*) override;

private:

    std::map<const G4BiasingProcessInterface*, G4BOptnChangeCrossSection*
    > fOperations;

    const G4ParticleDefinition* fParticleToBias;

    std::set<G4String> fProcessesToBias;

    std::map<G4String, G4double> fBiasFactors;
    class BiasingMessenger* fMessenger;
        
    G4bool fSetup;
};

#endif